from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

try:
    from .crawl_policy_sources import (
        ExtractedPage,
        load_policy_evidence,
        store_link_validations,
    )
    from .source_relevance import (
        SAME_NAME_VERSION_GUARD_VERSION,
        PolicyEvidence,
        VALIDATOR_VERSION,
    )
    from .verify_ai_evidence import AI_REVIEWER_VERSION, policy_evidence_hash
except ImportError:  # Direct execution: python etl/revalidate_policy_sources.py
    from crawl_policy_sources import (
        ExtractedPage,
        load_policy_evidence,
        store_link_validations,
    )
    from source_relevance import (
        SAME_NAME_VERSION_GUARD_VERSION,
        PolicyEvidence,
        VALIDATOR_VERSION,
    )
    from verify_ai_evidence import AI_REVIEWER_VERSION, policy_evidence_hash


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
CURRENT_AI_VALIDATOR_VERSION = f"{VALIDATOR_VERSION}+{AI_REVIEWER_VERSION}"
STALE_AI_VALIDATOR_VERSION = f"{VALIDATOR_VERSION}+ai-evidence-stale-v1"


@dataclass(frozen=True)
class RevalidationCandidate:
    source_id: Any
    document_id: Any
    final_url: str
    http_status: int | None
    title: str
    extracted_text: str
    metadata: dict[str, Any]
    policy_nos: tuple[str, ...]


@dataclass(frozen=True)
class StaleAiReviewTarget:
    ai_review_id: Any
    policy_no: str
    source_id: Any
    document_id: Any
    content_hash: str
    proposed_status: str
    observed_policy_evidence_hash: str
    current_policy_evidence_hash: str


def extracted_page_from_candidate(
    candidate: RevalidationCandidate,
) -> ExtractedPage:
    """Rebuild the exact extraction evidence stored with a document."""
    metadata = candidate.metadata
    return ExtractedPage(
        title=candidate.title,
        h1=str(metadata.get("h1") or ""),
        headings=tuple(
            str(value)
            for value in (metadata.get("headings") or ())
            if value is not None and str(value).strip()
        ),
        text=candidate.extracted_text,
        content_hash="",
        retrieval=dict(metadata.get("retrieval") or {}),
    )


def load_stale_ai_review_targets(
    conn: psycopg.Connection[Any],
    *,
    limit: int,
) -> list[StaleAiReviewTarget]:
    """Find applied AI decisions whose API-owned policy evidence drifted.

    The hash is recomputed in Python with the same canonical helper used by
    shadow review and apply. Fetching all currently applied AI rows avoids a
    pagination trap where unchanged rows at the front could permanently hide
    a stale row behind them; only the stale result is limited.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (
                review.policy_no,
                review.source_id,
                review.document_id
            )
                review.ai_review_id,
                review.policy_no,
                review.source_id,
                review.document_id,
                review.content_hash,
                review.proposed_status,
                review.policy_evidence_hash AS observed_policy_evidence_hash,
                policy_row.policy_name,
                ARRAY_REMOVE(ARRAY[
                    policy_row.supervising_org_name,
                    policy_row.operating_org_name,
                    policy_row.registrar_org_name
                ], NULL) AS organization_names,
                ARRAY(
                    SELECT DISTINCT resolved.region_name
                    FROM v_policy_region_resolved AS resolved
                    WHERE resolved.policy_no = policy_row.policy_no
                      AND resolved.resolution_type <> 'UNRESOLVED'
                      AND resolved.is_active = TRUE
                    ORDER BY resolved.region_name
                ) AS region_names,
                ARRAY(
                    SELECT TO_CHAR(period.start_date, 'YYYY-MM-DD')
                           || ' ~ '
                           || TO_CHAR(period.end_date, 'YYYY-MM-DD')
                    FROM policy_application_period AS period
                    WHERE period.policy_no = policy_row.policy_no
                    ORDER BY period.period_index
                ) AS application_periods,
                COALESCE(policy_row.keywords, '{}'::TEXT[])
                  || COALESCE(policy_row.middle_categories, '{}'::TEXT[])
                    AS distinctive_terms
            FROM policy_web_ai_review AS review
            JOIN policy_web_source AS link
              ON link.policy_no = review.policy_no
             AND link.source_id = review.source_id
             AND link.validated_document_id = review.document_id
             AND link.is_active = TRUE
             AND link.reviewed_at IS NULL
             AND link.validation_status = review.proposed_status
             AND link.validator_version = %s
            JOIN policy AS policy_row
              ON policy_row.policy_no = review.policy_no
             AND policy_row.is_active = TRUE
            JOIN web_document AS document
              ON document.document_id = review.document_id
             AND document.source_id = review.source_id
             AND document.content_hash = review.content_hash
             AND document.is_current = TRUE
            WHERE review.applied_at IS NOT NULL
              AND review.execution_status = 'SUCCEEDED'
              AND review.verification_result = 'ACCEPTED'
              AND review.proposed_status IN ('VERIFIED', 'REJECTED')
            ORDER BY
                review.policy_no,
                review.source_id,
                review.document_id,
                review.applied_at DESC,
                review.ai_review_id DESC
            """,
            (CURRENT_AI_VALIDATOR_VERSION,),
        )
        rows = list(cur.fetchall())

    stale: list[StaleAiReviewTarget] = []
    for row in rows:
        current_hash = policy_evidence_hash(PolicyEvidence.from_mapping(row))
        if current_hash == row["observed_policy_evidence_hash"]:
            continue
        stale.append(
            StaleAiReviewTarget(
                ai_review_id=row["ai_review_id"],
                policy_no=row["policy_no"],
                source_id=row["source_id"],
                document_id=row["document_id"],
                content_hash=row["content_hash"],
                proposed_status=row["proposed_status"],
                observed_policy_evidence_hash=row[
                    "observed_policy_evidence_hash"
                ],
                current_policy_evidence_hash=current_hash,
            )
        )
        if len(stale) >= limit:
            break
    return stale


def _load_locked_policy_evidence(
    conn: psycopg.Connection[Any],
    *,
    policy_no: str,
) -> PolicyEvidence:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                policy_row.policy_no,
                policy_row.policy_name,
                ARRAY_REMOVE(ARRAY[
                    policy_row.supervising_org_name,
                    policy_row.operating_org_name,
                    policy_row.registrar_org_name
                ], NULL) AS organization_names,
                ARRAY(
                    SELECT DISTINCT resolved.region_name
                    FROM v_policy_region_resolved AS resolved
                    WHERE resolved.policy_no = policy_row.policy_no
                      AND resolved.resolution_type <> 'UNRESOLVED'
                      AND resolved.is_active = TRUE
                    ORDER BY resolved.region_name
                ) AS region_names,
                ARRAY(
                    SELECT TO_CHAR(period.start_date, 'YYYY-MM-DD')
                           || ' ~ '
                           || TO_CHAR(period.end_date, 'YYYY-MM-DD')
                    FROM policy_application_period AS period
                    WHERE period.policy_no = policy_row.policy_no
                    ORDER BY period.period_index
                ) AS application_periods,
                COALESCE(policy_row.keywords, '{}'::TEXT[])
                  || COALESCE(policy_row.middle_categories, '{}'::TEXT[])
                    AS distinctive_terms
            FROM policy AS policy_row
            WHERE policy_row.policy_no = %s
              AND policy_row.is_active = TRUE
            FOR SHARE OF policy_row
            """,
            (policy_no,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("The AI-reviewed policy is missing or inactive")
    return PolicyEvidence.from_mapping(row)


def invalidate_stale_ai_review(
    conn: psycopg.Connection[Any],
    *,
    target: StaleAiReviewTarget,
) -> int:
    """Demote one stale applied AI decision without touching human review."""
    policy = _load_locked_policy_evidence(conn, policy_no=target.policy_no)
    current_hash = policy_evidence_hash(policy)
    if current_hash == target.observed_policy_evidence_hash:
        return 0

    with conn.cursor(row_factory=dict_row) as cur:
        # Follow the same policy -> source -> document -> relationship order
        # used by explicit AI apply and manual review.
        cur.execute(
            "SELECT source_id FROM web_source WHERE source_id = %s FOR UPDATE",
            (target.source_id,),
        )
        if cur.fetchone() is None:
            return 0
        cur.execute(
            """
            SELECT document_id
            FROM web_document
            WHERE document_id = %s
              AND source_id = %s
              AND content_hash = %s
              AND is_current = TRUE
            FOR UPDATE
            """,
            (target.document_id, target.source_id, target.content_hash),
        )
        if cur.fetchone() is None:
            return 0
        cur.execute(
            """
            SELECT relation_type, validation_status, validated_document_id,
                   validator_version, reviewed_at
            FROM policy_web_source
            WHERE policy_no = %s
              AND source_id = %s
              AND is_active = TRUE
            ORDER BY relation_type
            FOR UPDATE OF policy_web_source
            """,
            (target.policy_no, target.source_id),
        )
        links = list(cur.fetchall())

        if not links:
            return 0
        if any(link["reviewed_at"] is not None for link in links):
            return 0
        if any(
            link["validation_status"] != target.proposed_status
            or str(link["validated_document_id"]) != str(target.document_id)
            or link["validator_version"] != CURRENT_AI_VALIDATOR_VERSION
            for link in links
        ):
            return 0

        cur.execute(
            """
            UPDATE policy_web_source
            SET validation_status = 'REVIEW_REQUIRED',
                validation_score = NULL,
                matched_signals = '[]'::jsonb,
                conflicting_signals = '["ai_policy_evidence_stale"]'::jsonb,
                validation_reason = %s,
                validator_version = %s,
                validated_at = NOW()
            WHERE policy_no = %s
              AND source_id = %s
              AND is_active = TRUE
              AND validation_status = %s
              AND validated_document_id = %s
              AND reviewed_at IS NULL
              AND validator_version = %s
            RETURNING relation_type
            """,
            (
                "Applied AI evidence was invalidated because the current "
                "policy evidence hash changed.",
                STALE_AI_VALIDATOR_VERSION,
                target.policy_no,
                target.source_id,
                target.proposed_status,
                target.document_id,
                CURRENT_AI_VALIDATOR_VERSION,
            ),
        )
        updated = {row["relation_type"] for row in cur.fetchall()}
    expected = {link["relation_type"] for link in links}
    if updated != expected:
        raise RuntimeError("The AI-reviewed relationship changed during invalidation")
    return len(updated)


def load_revalidation_candidates(
    conn: psycopg.Connection[Any],
    *,
    limit: int,
) -> list[RevalidationCandidate]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                source.source_id,
                document.document_id,
                COALESCE(source.final_url, source.canonical_url) AS final_url,
                source.http_status,
                COALESCE(document.title, '') AS title,
                document.extracted_text,
                document.metadata,
                ARRAY_AGG(DISTINCT link.policy_no ORDER BY link.policy_no)
                    AS policy_nos
            FROM web_source AS source
            JOIN web_document AS document
              ON document.source_id = source.source_id
             AND document.is_current = TRUE
            JOIN policy_web_source AS link
              ON link.source_id = source.source_id
             AND link.is_active = TRUE
             AND link.reviewed_at IS NULL
            JOIN policy AS policy_row
              ON policy_row.policy_no = link.policy_no
             AND policy_row.is_active = TRUE
            WHERE link.validation_status = 'PENDING'
               OR link.validator_version IS NULL
               OR link.validator_version NOT IN (%s, %s, %s, %s)
               OR link.validated_document_id IS DISTINCT FROM document.document_id
               OR (
                    link.validator_version IS DISTINCT FROM %s
                    AND EXISTS (
                        SELECT 1
                        FROM policy_web_source AS sibling_link
                        JOIN policy AS sibling_policy
                          ON sibling_policy.policy_no = sibling_link.policy_no
                         AND sibling_policy.is_active = TRUE
                        WHERE sibling_link.source_id = link.source_id
                          AND sibling_link.is_active = TRUE
                          AND sibling_link.policy_no <> link.policy_no
                          AND LOWER(BTRIM(sibling_policy.policy_name))
                              = LOWER(BTRIM(policy_row.policy_name))
                    )
               )
            GROUP BY
                source.source_id,
                document.document_id,
                source.final_url,
                source.canonical_url,
                source.http_status
            ORDER BY source.source_id
            LIMIT %s
            """,
            (
                VALIDATOR_VERSION,
                CURRENT_AI_VALIDATOR_VERSION,
                STALE_AI_VALIDATOR_VERSION,
                SAME_NAME_VERSION_GUARD_VERSION,
                SAME_NAME_VERSION_GUARD_VERSION,
                limit,
            ),
        )
        return [
            RevalidationCandidate(
                source_id=row["source_id"],
                document_id=row["document_id"],
                final_url=row["final_url"],
                http_status=row["http_status"],
                title=row["title"],
                extracted_text=row["extracted_text"],
                metadata=dict(row["metadata"] or {}),
                policy_nos=tuple(row["policy_nos"] or ()),
            )
            for row in cur.fetchall()
        ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-run the deterministic relevance validator against current "
            "documents without making network requests."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="store revalidation results (default: plan only)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum source documents to revalidate (default: 100)",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    totals: dict[str, int] = {}

    with psycopg.connect(DB_DSN) as conn:
        stale_ai_targets = load_stale_ai_review_targets(
            conn,
            limit=args.limit,
        )
        remaining_limit = args.limit - len(stale_ai_targets)
        candidates = (
            load_revalidation_candidates(conn, limit=remaining_limit)
            if remaining_limit > 0
            else []
        )
        print(
            f"revalidation plan: stale_ai={len(stale_ai_targets)}, "
            f"documents={len(candidates)}, "
            f"validator={VALIDATOR_VERSION}"
        )
        for target in stale_ai_targets:
            print(
                f"stale_ai_review={target.ai_review_id} "
                f"policy_no={target.policy_no} "
                f"document_id={target.document_id}"
            )
        for candidate in candidates:
            print(
                f"{candidate.document_id} policies={len(candidate.policy_nos)}"
            )

        if not args.write:
            print("plan only; add --write to store validation results")
            return 0
        if not stale_ai_targets and not candidates:
            print("revalidation complete: no changes")
            return 0

        conn.commit()
        for target in stale_ai_targets:
            with conn.transaction():
                invalidated = invalidate_stale_ai_review(
                    conn,
                    target=target,
                )
            if invalidated:
                totals["AI_STALE_REVIEW_REQUIRED"] = (
                    totals.get("AI_STALE_REVIEW_REQUIRED", 0) + invalidated
                )

        for candidate in candidates:
            extracted = extracted_page_from_candidate(candidate)
            policies = [
                policy
                for policy in load_policy_evidence(conn, candidate.source_id)
                if policy.policy_no in candidate.policy_nos
            ]
            conn.commit()
            with conn.transaction():
                counts = store_link_validations(
                    conn,
                    source_id=candidate.source_id,
                    document_id=candidate.document_id,
                    final_url=candidate.final_url,
                    http_status=candidate.http_status,
                    extracted=extracted,
                    policies=policies,
                )
            for status, count in counts.items():
                totals[status] = totals.get(status, 0) + count

    if not totals:
        print("revalidation complete: no changes")
        return 0

    print(
        "revalidation complete: "
        + ", ".join(f"{status}={count}" for status, count in sorted(totals.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
