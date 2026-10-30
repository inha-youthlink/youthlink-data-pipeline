from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

try:
    from .embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        count_tokens,
        create_openai_client,
        get_token_encoder,
        is_retryable_error,
        request_embeddings,
        suggested_retry_delay,
        vector_literal,
    )
except ImportError:  # Direct execution: python etl/embed_policy_web_chunks.py
    from embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        count_tokens,
        create_openai_client,
        get_token_encoder,
        is_retryable_error,
        request_embeddings,
        suggested_retry_delay,
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
EMBEDDING_DIMENSIONS = DEFAULT_EMBEDDING_DIMENSIONS


@dataclass(frozen=True)
class Candidate:
    web_chunk_id: Any
    policy_no: str
    document_id: Any
    content: str
    token_count: int


def _batched(values: Sequence[Candidate], size: int) -> list[Sequence[Candidate]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def load_candidates(
    conn: psycopg.Connection[Any],
    *,
    encoder: Any,
    model: str,
    policy_nos: Sequence[str],
    limit: int | None,
    retry_errors: bool,
    force: bool,
) -> tuple[list[Candidate], int]:
    # The verified view excludes stale/unverified web documents. This query
    # never reads from or writes to the API-owned policy_chunk corpus.
    conditions: list[str] = []
    params: list[Any] = []
    if not force:
        conditions.append(
            "(chunk.embedding IS NULL OR chunk.embedding_model IS DISTINCT FROM %s)"
        )
        params.append(model)
    if not retry_errors and not force:
        conditions.append("chunk.embedding_error IS NULL")
    if policy_nos:
        conditions.append("verified.policy_no = ANY(%s)")
        params.append(list(policy_nos))

    sql = """
        SELECT chunk.web_chunk_id, chunk.policy_no, chunk.document_id,
               chunk.content
        FROM policy_web_chunk AS chunk
        JOIN v_verified_policy_web_chunk AS verified
          ON verified.web_chunk_id = chunk.web_chunk_id
    """
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY chunk.policy_no, chunk.document_id, chunk.chunk_index"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())

    candidates = [
        Candidate(
            web_chunk_id=row["web_chunk_id"],
            policy_no=row["policy_no"],
            document_id=row["document_id"],
            content=row["content"],
            token_count=count_tokens(row["content"], encoder),
        )
        for row in rows
    ]
    return candidates, sum(candidate.token_count for candidate in candidates)


def request_with_retry(
    client: Any,
    texts: Sequence[str],
    *,
    model: str,
    max_attempts: int,
    base_delay: float,
) -> tuple[list[list[float]], int]:
    for attempt in range(1, max_attempts + 1):
        try:
            return request_embeddings(
                client,
                texts,
                model=model,
                dimensions=EMBEDDING_DIMENSIONS,
            )
        except Exception as error:
            if attempt == max_attempts or not is_retryable_error(error):
                raise
            exponential_delay = base_delay * (2 ** (attempt - 1))
            server_delay = suggested_retry_delay(error) or 0.0
            delay = min(max(exponential_delay, server_delay + 0.5), 30.0)
            print(
                f"temporary web embedding error ({type(error).__name__}); "
                f"retrying in {delay:g}s ({attempt}/{max_attempts})",
                flush=True,
            )
            time.sleep(delay)

    raise AssertionError("unreachable")


def store_success(
    conn: psycopg.Connection[Any],
    batch: Sequence[Candidate],
    vectors: Sequence[Sequence[float]],
    model: str,
) -> None:
    rows = [
        (
            vector_literal(vector, EMBEDDING_DIMENSIONS),
            model,
            candidate.token_count,
            candidate.web_chunk_id,
        )
        for candidate, vector in zip(batch, vectors, strict=True)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE policy_web_chunk
            SET embedding = %s::vector,
                embedding_model = %s,
                token_count = %s,
                embedded_at = NOW(),
                embedding_error = NULL,
                updated_at = NOW()
            WHERE web_chunk_id = %s
            """,
            rows,
        )
    conn.commit()


def store_failure(
    conn: psycopg.Connection[Any],
    batch: Sequence[Candidate],
    error: Exception,
) -> None:
    message = f"{type(error).__name__}: {error}"[:4000]
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE policy_web_chunk
            SET embedding_error = %s,
                updated_at = NOW()
            WHERE web_chunk_id = %s
            """,
            [(message, candidate.web_chunk_id) for candidate in batch],
        )
    conn.commit()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Embed only verified crawled-web fallback chunks. API policy_chunk "
            "rows are never modified."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="call the API and update policy_web_chunk (default: plan only)",
    )
    parser.add_argument(
        "--policy-no",
        action="append",
        default=[],
        help="embed web chunks for this policy only; may be repeated",
    )
    parser.add_argument("--limit", type=int, help="maximum web chunks")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--base-delay", type=float, default=1.0)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than zero")
    if args.max_attempts <= 0:
        parser.error("--max-attempts must be greater than zero")
    if args.base_delay < 0:
        parser.error("--base-delay must not be negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    encoder = get_token_encoder()

    with psycopg.connect(DB_DSN) as conn:
        candidates, estimated_tokens = load_candidates(
            conn,
            encoder=encoder,
            model=EMBEDDING_MODEL,
            policy_nos=args.policy_no,
            limit=args.limit,
            retry_errors=args.retry_errors,
            force=args.force,
        )
        print(
            f"web embedding plan: chunks={len(candidates)}, "
            f"estimated_tokens={estimated_tokens}, model={EMBEDDING_MODEL}, "
            f"dimensions={EMBEDDING_DIMENSIONS}",
            flush=True,
        )
        if not args.write or not candidates:
            if not args.write:
                print(
                    "plan only; add --write to embed separate web chunks",
                    flush=True,
                )
            return 0

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when --write is used")
        client = create_openai_client(api_key)

        succeeded = 0
        failed = 0
        for batch_number, batch in enumerate(
            _batched(candidates, args.batch_size),
            start=1,
        ):
            try:
                vectors, used_tokens = request_with_retry(
                    client,
                    [candidate.content for candidate in batch],
                    model=EMBEDDING_MODEL,
                    max_attempts=args.max_attempts,
                    base_delay=args.base_delay,
                )
                store_success(conn, batch, vectors, EMBEDDING_MODEL)
                succeeded += len(batch)
                print(
                    f"batch {batch_number}: embedded={len(batch)}, "
                    f"api_tokens={used_tokens}",
                    flush=True,
                )
            except Exception as error:
                conn.rollback()
                store_failure(conn, batch, error)
                failed += len(batch)
                print(
                    f"batch {batch_number}: failed={len(batch)} "
                    f"({type(error).__name__})",
                    flush=True,
                )

    print(
        f"web embedding complete: succeeded={succeeded}, failed={failed}"
    )
    print("API-owned policy and policy_chunk rows were not modified")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
