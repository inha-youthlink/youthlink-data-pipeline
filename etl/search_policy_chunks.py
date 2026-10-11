from __future__ import annotations

import argparse
import os
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

try:
    from .embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        create_openai_client,
        request_embeddings,
        vector_literal,
    )
except ImportError:  # Direct execution: python etl/search_policy_chunks.py
    from embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        create_openai_client,
        request_embeddings,
        vector_literal,
    )


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
EMBEDDING_MODEL = os.getenv(
    "OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
).strip()


def search_chunks(
    conn: psycopg.Connection[Any],
    query_vector: Sequence[float],
    model: str,
    limit: int,
    policy_nos: Sequence[str] = (),
    chunk_types: Sequence[str] = (),
) -> list[dict[str, Any]]:
    vector = vector_literal(query_vector, DEFAULT_EMBEDDING_DIMENSIONS)
    conditions = ["pc.embedding IS NOT NULL", "pc.embedding_model = %s"]
    params: list[Any] = [model]
    if policy_nos:
        conditions.append("pc.policy_no = ANY(%s)")
        params.append(list(policy_nos))
    if chunk_types:
        conditions.append("pc.chunk_type = ANY(%s)")
        params.append(list(chunk_types))

    params.extend([vector, limit])
    sql = f"""
        SELECT
            pc.chunk_id,
            pc.policy_no,
            p.policy_name,
            pc.chunk_type,
            pc.content,
            1 - (pc.embedding <=> %s::vector) AS similarity
        FROM policy_chunk pc
        JOIN policy p ON p.policy_no = pc.policy_no
        WHERE {' AND '.join(conditions)}
        ORDER BY pc.embedding <=> %s::vector
        LIMIT %s
    """

    # The query vector is used by SELECT and ORDER BY. Insert the first copy
    # before the WHERE parameters, then retain the second copy at the end.
    query_params = [vector, *params[:-2], vector, params[-1]]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, query_params)
        return list(cur.fetchall())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search embedded YouthLink policy chunks with pgvector."
    )
    parser.add_argument("query", help="natural-language search query")
    parser.add_argument("--limit", type=int, default=10, help="result count")
    parser.add_argument(
        "--policy-no",
        action="append",
        default=[],
        help="restrict candidates to a policy number; may be repeated",
    )
    parser.add_argument(
        "--chunk-type",
        action="append",
        choices=["SUMMARY", "ELIGIBILITY", "SUPPORT", "APPLICATION"],
        default=[],
        help="restrict candidates to a chunk type; may be repeated",
    )
    parser.add_argument(
        "--full-content",
        action="store_true",
        help="print full chunk content instead of a 300-character preview",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be greater than zero")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to embed the search query")

    client = create_openai_client(api_key)
    vectors, _ = request_embeddings(
        client,
        [args.query],
        model=EMBEDDING_MODEL,
        dimensions=DEFAULT_EMBEDDING_DIMENSIONS,
    )

    with psycopg.connect(DB_DSN) as conn:
        results = search_chunks(
            conn,
            vectors[0],
            model=EMBEDDING_MODEL,
            limit=args.limit,
            policy_nos=args.policy_no,
            chunk_types=args.chunk_type,
        )

    if not results:
        print("No embedded chunks matched the selected model and filters.")
        return 0

    for rank, row in enumerate(results, start=1):
        content = row["content"]
        if not args.full_content and len(content) > 300:
            content = content[:300].rstrip() + "..."
        print(
            f"\n{rank}. {row['policy_name']} "
            f"[{row['policy_no']} / {row['chunk_type']}] "
            f"similarity={row['similarity']:.4f}\n{content}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
