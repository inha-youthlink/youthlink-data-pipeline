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
    from .source_relevance import VALIDATOR_VERSION
except ImportError:  # Direct execution: python etl/revalidate_policy_sources.py
    from crawl_policy_sources import (
        ExtractedPage,
        load_policy_evidence,
        store_link_validations,
    )
    from source_relevance import VALIDATOR_VERSION


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)


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
               OR link.validator_version IS DISTINCT FROM %s
               OR link.validated_document_id IS DISTINCT FROM document.document_id
            GROUP BY
                source.source_id,
                document.document_id,
                source.final_url,
                source.canonical_url,
                source.http_status
            ORDER BY source.source_id
            LIMIT %s
            """,
            (VALIDATOR_VERSION, limit),
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
        candidates = load_revalidation_candidates(conn, limit=args.limit)
        print(
            f"revalidation plan: documents={len(candidates)}, "
            f"validator={VALIDATOR_VERSION}"
        )
        for candidate in candidates:
            print(
                f"{candidate.document_id} policies={len(candidate.policy_nos)}"
            )

        if not args.write or not candidates:
            if not args.write:
                print("plan only; add --write to store validation results")
            return 0

        conn.commit()
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

    print(
        "revalidation complete: "
        + ", ".join(f"{status}={count}" for status, count in sorted(totals.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
