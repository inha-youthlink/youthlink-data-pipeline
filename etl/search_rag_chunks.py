from __future__ import annotations

import argparse
import os
from typing import Any, Mapping, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

try:
    from .embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        create_embedding_client,
        request_embeddings,
        vector_literal,
    )
except ImportError:  # Direct execution: python etl/search_rag_chunks.py
    from embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        create_embedding_client,
        request_embeddings,
        vector_literal,
    )


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
EMBEDDING_MODEL = os.getenv(
    "LITELLM_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
).strip()
EMBEDDING_API_BASE_URL = os.getenv("LITELLM_BASE_URL", "").strip()

SOURCE_TYPES = ("API", "CRAWLED_WEB")
CHUNK_TYPES = (
    "SUMMARY",
    "ELIGIBILITY",
    "SUPPORT",
    "APPLICATION",
    "WEB_TEXT",
)


def search_rag_chunks(
    conn: psycopg.Connection[Any],
    query_vector: Sequence[float],
    model: str,
    limit: int,
    policy_nos: Sequence[str] = (),
    source_types: Sequence[str] = (),
    chunk_types: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Search the unified retrieval projection with one cosine KNN query."""
    if limit <= 0:
        raise ValueError("limit must be greater than zero")

    vector = vector_literal(query_vector, DEFAULT_EMBEDDING_DIMENSIONS)
    conditions = [
        "p.is_active = TRUE",
        "retrieval.embedding IS NOT NULL",
        "retrieval.embedding_model = %s",
    ]
    where_params: list[Any] = [model]
    if policy_nos:
        conditions.append("retrieval.policy_no = ANY(%s)")
        where_params.append(list(policy_nos))
    if source_types:
        conditions.append("retrieval.source_type = ANY(%s)")
        where_params.append(list(source_types))
    if chunk_types:
        conditions.append("retrieval.chunk_type = ANY(%s)")
        where_params.append(list(chunk_types))

    sql = f"""
        SELECT
            retrieval.retrieval_chunk_id,
            retrieval.policy_no,
            p.policy_name,
            retrieval.source_type,
            retrieval.policy_chunk_id,
            retrieval.web_chunk_id,
            retrieval.document_id,
            retrieval.source_id,
            COALESCE(retrieval.final_url, retrieval.canonical_url) AS source_url,
            retrieval.chunk_index,
            retrieval.chunk_type,
            retrieval.content,
            retrieval.source_priority,
            1 - (retrieval.embedding <=> %s::vector) AS similarity
        FROM rag_retrieval_chunk AS retrieval
        JOIN policy AS p ON p.policy_no = retrieval.policy_no
        WHERE {' AND '.join(conditions)}
        ORDER BY retrieval.embedding <=> %s::vector
        LIMIT %s
    """
    params = [vector, *where_params, vector, limit]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def format_result(row: Mapping[str, Any], rank: int, full_content: bool) -> str:
    content = str(row["content"])
    if not full_content and len(content) > 300:
        content = content[:300].rstrip() + "..."

    source_type = str(row["source_type"])
    lines = [
        (
            f"{rank}. {row['policy_name']} "
            f"[{row['policy_no']} / {row['chunk_type']} / {source_type}] "
            f"similarity={float(row['similarity']):.4f}"
        )
    ]
    if source_type == "API":
        lines.append(f"source policy_chunk_id={row.get('policy_chunk_id')}")
    else:
        lines.append(
            "source "
            f"web_chunk_id={row.get('web_chunk_id')} "
            f"document_id={row.get('document_id')} "
            f"url={row.get('source_url') or '-'}"
        )
    lines.append(content)
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Search the unified API and verified crawled-web RAG projection "
            "with one pgvector query."
        )
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
        "--source-type",
        action="append",
        choices=SOURCE_TYPES,
        default=[],
        help="restrict candidates to API or CRAWLED_WEB; may be repeated",
    )
    parser.add_argument(
        "--chunk-type",
        action="append",
        choices=CHUNK_TYPES,
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
    api_key = os.getenv("LITELLM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("LITELLM_API_KEY is required to embed the search query")
    if not EMBEDDING_API_BASE_URL:
        raise RuntimeError("LITELLM_BASE_URL is required to embed the search query")

    client = create_embedding_client(api_key, EMBEDDING_API_BASE_URL)
    vectors, _ = request_embeddings(
        client,
        [args.query],
        model=EMBEDDING_MODEL,
        dimensions=DEFAULT_EMBEDDING_DIMENSIONS,
    )

    with psycopg.connect(DB_DSN) as conn:
        results = search_rag_chunks(
            conn,
            vectors[0],
            model=EMBEDDING_MODEL,
            limit=args.limit,
            policy_nos=args.policy_no,
            source_types=args.source_type,
            chunk_types=args.chunk_type,
        )

    if not results:
        print("No embedded retrieval chunks matched the selected filters.")
        return 0

    for rank, row in enumerate(results, start=1):
        print("\n" + format_result(row, rank, args.full_content))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
