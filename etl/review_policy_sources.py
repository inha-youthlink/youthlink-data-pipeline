from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
MANUAL_VALIDATOR_VERSION = "manual-review-v1"
MANUAL_DECISIONS = ("VERIFIED", "REJECTED", "LINK_ONLY")


@dataclass(frozen=True)
class ReviewCandidate:
    policy_no: str
    policy_name: str
    source_id: Any
    host: str
    source_url: str
    document_id: Any
    document_title: str
    content_hash: str
    fetched_at: Any
    extracted_text: str
    relation_types: tuple[str, ...]
    validation_score: float | None
    matched_signals: tuple[str, ...]
    conflicting_signals: tuple[str, ...]
    validation_reason: str


def _clean_snippet(text: str, limit: int) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def load_review_candidates(
    conn: psycopg.Connection[Any],
    *,
    limit: int,
) -> list[ReviewCandidate]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                policy_row.policy_no,
                policy_row.policy_name,
                source.source_id,
                source.host,
                COALESCE(source.final_url, source.canonical_url) AS source_url,
                document.document_id,
                COALESCE(document.title, '') AS document_title,
                document.content_hash,
                document.fetched_at,
                document.extracted_text,
                ARRAY_AGG(DISTINCT link.relation_type ORDER BY link.relation_type)
                    AS relation_types,
                MIN(link.validation_score)::DOUBLE PRECISION AS validation_score,
                ARRAY(
                    SELECT DISTINCT signal.value
                    FROM policy_web_source AS signal_link,
                         LATERAL jsonb_array_elements_text(
                             signal_link.matched_signals
                         ) AS signal(value)
                    WHERE signal_link.policy_no = policy_row.policy_no
                      AND signal_link.source_id = source.source_id
                      AND signal_link.is_active = TRUE
                    ORDER BY signal.value
                ) AS matched_signals,
                ARRAY(
                    SELECT DISTINCT signal.value
                    FROM policy_web_source AS signal_link,
                         LATERAL jsonb_array_elements_text(
                             signal_link.conflicting_signals
                         ) AS signal(value)
                    WHERE signal_link.policy_no = policy_row.policy_no
                      AND signal_link.source_id = source.source_id
                      AND signal_link.is_active = TRUE
                    ORDER BY signal.value
                ) AS conflicting_signals,
                MIN(COALESCE(link.validation_reason, '')) AS validation_reason
            FROM policy_web_source AS link
            JOIN policy AS policy_row
              ON policy_row.policy_no = link.policy_no
             AND policy_row.is_active = TRUE
            JOIN web_source AS source
              ON source.source_id = link.source_id
            JOIN web_document AS document
              ON document.document_id = link.validated_document_id
             AND document.source_id = source.source_id
             AND document.is_current = TRUE
            WHERE link.is_active = TRUE
              AND link.validation_status = 'REVIEW_REQUIRED'
              AND link.reviewed_at IS NULL
            GROUP BY
                policy_row.policy_no,
                policy_row.policy_name,
                source.source_id,
                source.host,
                source.final_url,
                source.canonical_url,
                document.document_id
            ORDER BY source.host, policy_row.policy_no
            LIMIT %s
            """,
            (limit,),
        )
        return [
            ReviewCandidate(
                policy_no=row["policy_no"],
                policy_name=row["policy_name"],
                source_id=row["source_id"],
                host=row["host"],
                source_url=row["source_url"],
                document_id=row["document_id"],
                document_title=row["document_title"],
                content_hash=row["content_hash"],
                fetched_at=row["fetched_at"],
                extracted_text=row["extracted_text"],
                relation_types=tuple(row["relation_types"] or ()),
                validation_score=(
                    float(row["validation_score"])
                    if row["validation_score"] is not None
                    else None
                ),
                matched_signals=tuple(row["matched_signals"] or ()),
                conflicting_signals=tuple(row["conflicting_signals"] or ()),
                validation_reason=row["validation_reason"],
            )
            for row in cur.fetchall()
        ]


def load_locked_review_target(
    conn: psycopg.Connection[Any],
    *,
    policy_no: str,
    source_id: str,
    document_id: str,
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        # Lock the active policy first so a concurrent full sync cannot make
        # this decision visible for a policy that became inactive mid-review.
        cur.execute(
            """
            SELECT policy_no
            FROM policy
            WHERE policy_no = %s
              AND is_active = TRUE
            FOR SHARE
            """,
            (policy_no,),
        )
        if cur.fetchone() is None:
            raise RuntimeError("The target policy is missing or inactive")

        # The crawler locks web_source before web_document. Following that
        # order avoids deadlocks and freezes the source while review commits.
        cur.execute(
            """
            SELECT source_id
            FROM web_source
            WHERE source_id = %s
            FOR UPDATE
            """,
            (source_id,),
        )
        if cur.fetchone() is None:
            raise RuntimeError("The target web source does not exist")

        # Lock the exact document version, not merely its URL/source. The
        # crawler must wait before replacing is_current while review commits.
        cur.execute(
            """
            SELECT document_id
            FROM web_document
            WHERE document_id = %s
              AND source_id = %s
              AND is_current = TRUE
            FOR UPDATE
            """,
            (document_id, source_id),
        )
        if cur.fetchone() is None:
            raise RuntimeError(
                "The expected document does not belong to this source or "
                "is no longer current"
            )

        cur.execute(
            """
            SELECT
                link.relation_type,
                link.validation_status,
                link.reviewed_at,
                link.validated_document_id
            FROM policy_web_source AS link
            WHERE link.policy_no = %s
              AND link.source_id = %s
              AND link.is_active = TRUE
            ORDER BY link.relation_type
            FOR UPDATE OF link
            """,
            (policy_no, source_id),
        )
        rows = list(cur.fetchall())

    if not rows:
        raise RuntimeError("No active policy/source relationship matches the target")
    if any(row["reviewed_at"] is not None for row in rows):
        raise RuntimeError("The target relationship has already been manually reviewed")
    if any(row["validation_status"] != "REVIEW_REQUIRED" for row in rows):
        raise RuntimeError("Only REVIEW_REQUIRED relationships can be manually decided")
    if any(str(row["validated_document_id"]) != str(document_id) for row in rows):
        raise RuntimeError("The expected document is not the version that was validated")
    return rows


def apply_manual_review(
    conn: psycopg.Connection[Any],
    *,
    policy_no: str,
    source_id: str,
    document_id: str,
    decision: str,
    reviewer: str,
    reason: str,
) -> int:
    if decision not in MANUAL_DECISIONS:
        raise ValueError(f"Unsupported manual decision: {decision}")
    reviewer = reviewer.strip()
    reason = reason.strip()
    if not reviewer or len(reviewer) > 100:
        raise ValueError("reviewer must contain 1 to 100 characters")
    if not reason or len(reason) > 2000:
        raise ValueError("reason must contain 1 to 2000 characters")

    rows = load_locked_review_target(
        conn,
        policy_no=policy_no,
        source_id=source_id,
        document_id=document_id,
    )
    matched_addition = (
        ["manual_review_verified"]
        if decision == "VERIFIED"
        else ["manual_review_link_only"] if decision == "LINK_ONLY" else []
    )
    conflicting_addition = (
        ["manual_review_rejected"] if decision == "REJECTED" else []
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE policy_web_source
            SET validation_status = %s,
                validation_score = NULL,
                matched_signals = matched_signals || %s,
                conflicting_signals = conflicting_signals || %s,
                validation_reason = %s,
                validator_version = %s,
                validated_at = NOW(),
                validated_document_id = %s,
                reviewed_by = %s,
                reviewed_at = NOW()
            WHERE policy_no = %s
              AND source_id = %s
              AND is_active = TRUE
              AND validation_status = 'REVIEW_REQUIRED'
              AND validated_document_id = %s
              AND reviewed_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM policy AS policy_row
                  WHERE policy_row.policy_no = policy_web_source.policy_no
                    AND policy_row.is_active = TRUE
              )
              AND EXISTS (
                  SELECT 1
                  FROM web_document AS document
                  WHERE document.document_id = %s
                    AND document.source_id = policy_web_source.source_id
                    AND document.is_current = TRUE
              )
            RETURNING relation_type
            """,
            (
                decision,
                Jsonb(matched_addition),
                Jsonb(conflicting_addition),
                f"Manual review: {reason}",
                MANUAL_VALIDATOR_VERSION,
                document_id,
                reviewer,
                policy_no,
                source_id,
                document_id,
                document_id,
            ),
        )
        updated_relation_types = {row[0] for row in cur.fetchall()}
    expected_relation_types = {row["relation_type"] for row in rows}
    if updated_relation_types != expected_relation_types:
        raise RuntimeError(
            "The relationship changed during review; no partial decision is allowed"
        )
    return len(updated_relation_types)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List or manually decide REVIEW_REQUIRED policy web sources. "
            "API policy and policy_chunk rows are never modified."
        )
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--snippet-chars", type=int, default=500)
    parser.add_argument("--show-url", action="store_true")
    parser.add_argument("--policy-no")
    parser.add_argument("--source-id", type=UUID)
    parser.add_argument(
        "--expected-document-id",
        "--document-id",
        dest="expected_document_id",
        type=UUID,
        help=(
            "exact current web_document version that was inspected; "
            "--document-id is accepted as a compatibility alias"
        ),
    )
    parser.add_argument("--decision", choices=MANUAL_DECISIONS)
    parser.add_argument("--reviewed-by")
    parser.add_argument("--reason")
    parser.add_argument(
        "--write",
        action="store_true",
        help="store the manual decision (default: list or validate only)",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.snippet_chars <= 0:
        parser.error("--snippet-chars must be greater than zero")

    action_values = (
        args.policy_no,
        args.source_id,
        args.expected_document_id,
        args.decision,
        args.reviewed_by,
        args.reason,
    )
    has_action = any(value is not None for value in action_values)
    if has_action and not all(value is not None for value in action_values):
        parser.error(
            "manual review requires --policy-no, --source-id, "
            "--expected-document-id, --decision, --reviewed-by, and "
            "--reason together"
        )
    if args.write and not has_action:
        parser.error("--write requires a complete manual review target")
    if args.reviewed_by is not None:
        args.reviewed_by = args.reviewed_by.strip()
        if not args.reviewed_by or len(args.reviewed_by) > 100:
            parser.error("--reviewed-by must contain 1 to 100 characters")
    if args.reason is not None:
        args.reason = args.reason.strip()
        if not args.reason or len(args.reason) > 2000:
            parser.error("--reason must contain 1 to 2000 characters")
    if args.policy_no is not None:
        args.policy_no = args.policy_no.strip()
        if not args.policy_no or len(args.policy_no) > 30:
            parser.error("--policy-no must contain 1 to 30 characters")
    return args


def print_candidates(
    candidates: Sequence[ReviewCandidate],
    *,
    snippet_chars: int,
    show_url: bool,
) -> None:
    print(f"manual review candidates={len(candidates)}")
    for candidate in candidates:
        print(
            f"\npolicy_no={candidate.policy_no}\n"
            f"policy_name={candidate.policy_name}\n"
            f"source_id={candidate.source_id}\n"
            f"document_id={candidate.document_id}\n"
            f"host={candidate.host}\n"
            f"relations={','.join(candidate.relation_types)}\n"
            f"document_title={candidate.document_title}\n"
            f"content_hash={candidate.content_hash}\n"
            f"fetched_at={candidate.fetched_at}\n"
            f"score={candidate.validation_score}\n"
            f"matched={','.join(candidate.matched_signals) or '-'}\n"
            f"conflicts={','.join(candidate.conflicting_signals) or '-'}\n"
            f"reason={candidate.validation_reason}\n"
            f"snippet={_clean_snippet(candidate.extracted_text, snippet_chars)}"
        )
        if show_url:
            print(f"source_url={candidate.source_url}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    has_action = args.decision is not None
    with psycopg.connect(DB_DSN) as conn:
        if not has_action:
            candidates = load_review_candidates(conn, limit=args.limit)
            print_candidates(
                candidates,
                snippet_chars=args.snippet_chars,
                show_url=args.show_url,
            )
            return 0

        if not args.write:
            rows = load_locked_review_target(
                conn,
                policy_no=args.policy_no,
                source_id=args.source_id,
                document_id=args.expected_document_id,
            )
            print(
                f"review target valid: policy_no={args.policy_no}, "
                f"document_id={args.expected_document_id}, "
                f"relationships={len(rows)}, decision={args.decision}"
            )
            conn.rollback()
            print("dry-run only; add --write after checking the exact document")
            return 0

        with conn.transaction():
            updated = apply_manual_review(
                conn,
                policy_no=args.policy_no,
                source_id=args.source_id,
                document_id=args.expected_document_id,
                decision=args.decision,
                reviewer=args.reviewed_by,
                reason=args.reason,
            )

    print(f"manual review stored: relationships={updated}")
    print("API-owned policy and policy_chunk rows were not modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
