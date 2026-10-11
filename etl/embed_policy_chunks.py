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
except ImportError:  # Direct execution: python etl/embed_policy_chunks.py
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
    chunk_id: Any
    policy_no: str
    chunk_type: str
    content: str
    token_count: int


def _batched(values: Sequence[Candidate], size: int) -> list[Sequence[Candidate]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def load_candidates(
    conn: psycopg.Connection[Any],
    encoder: Any,
    model: str,
    limit: int | None,
    retry_errors: bool,
    force: bool,
) -> tuple[list[Candidate], int]:
    conditions: list[str] = []
    params: list[Any] = []

    if not force:
        conditions.append("(embedding IS NULL OR embedding_model IS DISTINCT FROM %s)")
        params.append(model)
    if not retry_errors and not force:
        conditions.append("embedding_error IS NULL")

    sql = """
        SELECT chunk_id, policy_no, chunk_type, content
        FROM policy_chunk
    """
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY policy_no, chunk_index"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())

    candidates = [
        Candidate(
            chunk_id=row["chunk_id"],
            policy_no=row["policy_no"],
            chunk_type=row["chunk_type"],
            content=row["content"],
            token_count=count_tokens(row["content"], encoder),
        )
        for row in rows
    ]
    return candidates, sum(item.token_count for item in candidates)


def request_with_retry(
    client: Any,
    texts: Sequence[str],
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
                f"temporary embedding error ({type(error).__name__}); "
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
            candidate.chunk_id,
        )
        for candidate, vector in zip(batch, vectors, strict=True)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE policy_chunk
            SET embedding = %s::vector,
                embedding_model = %s,
                token_count = %s,
                embedded_at = NOW(),
                embedding_error = NULL
            WHERE chunk_id = %s
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
            UPDATE policy_chunk
            SET embedding_error = %s
            WHERE chunk_id = %s
            """,
            [(message, candidate.chunk_id) for candidate in batch],
        )
    conn.commit()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create OpenAI embeddings for policy_chunk rows."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="call the API and store embeddings (default: plan only)",
    )
    parser.add_argument("--limit", type=int, help="maximum chunks to process")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="inputs per API request (default: 100)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=8,
        help="maximum attempts for temporary API failures (default: 8)",
    )
    parser.add_argument(
        "--base-delay",
        type=float,
        default=1.0,
        help="initial retry delay in seconds (default: 1)",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="include chunks with a previously recorded embedding_error",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-embed every selected chunk, including valid current vectors",
    )
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
            limit=args.limit,
            retry_errors=args.retry_errors,
            force=args.force,
        )

        print(
            f"embedding plan: chunks={len(candidates)}, "
            f"estimated_tokens={estimated_tokens}, model={EMBEDDING_MODEL}, "
            f"dimensions={EMBEDDING_DIMENSIONS}",
            flush=True,
        )
        if not args.write or not candidates:
            if not args.write:
                print(
                    "plan only; add --write to call the API and update the database",
                    flush=True,
                )
            return 0

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when --write is used")
        client = create_openai_client(api_key)

        succeeded = 0
        failed = 0
        actual_tokens = 0
        batches = _batched(candidates, args.batch_size)
        for batch_number, batch in enumerate(batches, start=1):
            try:
                vectors, prompt_tokens = request_with_retry(
                    client,
                    [candidate.content for candidate in batch],
                    model=EMBEDDING_MODEL,
                    max_attempts=args.max_attempts,
                    base_delay=args.base_delay,
                )
                store_success(conn, batch, vectors, EMBEDDING_MODEL)
                succeeded += len(batch)
                actual_tokens += prompt_tokens
                print(
                    f"batch {batch_number}/{len(batches)}: "
                    f"embedded={len(batch)}, prompt_tokens={prompt_tokens}",
                    flush=True,
                )
            except Exception as error:
                conn.rollback()
                store_failure(conn, batch, error)
                failed += len(batch)
                print(
                    f"batch {batch_number}/{len(batches)} failed: "
                    f"{type(error).__name__}: {error}",
                    flush=True,
                )

    print(
        f"embedding complete: succeeded={succeeded}, failed={failed}, "
        f"prompt_tokens={actual_tokens}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
