from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        DEFAULT_EMBEDDING_ENCODING_FORMAT,
        DEFAULT_EMBEDDING_MODEL,
        create_embedding_client,
        estimate_token_count,
        is_retryable_error,
        request_embeddings,
        suggested_retry_delay,
        vector_literal,
    )
except ImportError:  # Direct execution: python etl/embed_policy_chunks.py
    from embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_ENCODING_FORMAT,
        DEFAULT_EMBEDDING_MODEL,
        create_embedding_client,
        estimate_token_count,
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
    "LITELLM_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
).strip()
EMBEDDING_API_BASE_URL = os.getenv("LITELLM_BASE_URL", "").strip()
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
    model: str,
    limit: int | None,
    retry_errors: bool,
    force: bool,
) -> tuple[list[Candidate], int]:
    # Policy rows are soft-deleted with is_active rather than physically
    # removed. Keep their existing vectors, but never spend API tokens creating
    # or refreshing embeddings that cannot appear in retrieval results.
    conditions: list[str] = ["p.is_active = TRUE"]
    params: list[Any] = []

    if not force:
        conditions.append(
            "(pc.embedding IS NULL OR pc.embedding_model IS DISTINCT FROM %s "
            "OR pc.embedding_encoding_format IS DISTINCT FROM %s)"
        )
        params.extend([model, DEFAULT_EMBEDDING_ENCODING_FORMAT])
    if not retry_errors and not force:
        conditions.append("pc.embedding_error IS NULL")

    sql = """
        SELECT pc.chunk_id, pc.policy_no, pc.chunk_type, pc.content
        FROM policy_chunk pc
        JOIN policy p ON p.policy_no = pc.policy_no
    """
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY pc.policy_no, pc.chunk_index"
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
            token_count=estimate_token_count(row["content"]),
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
            DEFAULT_EMBEDDING_ENCODING_FORMAT,
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
                embedding_encoding_format = %s,
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
            SET embedding = NULL,
                embedding_model = NULL,
                embedding_encoding_format = NULL,
                token_count = NULL,
                embedded_at = NULL,
                embedding_error = %s
            WHERE chunk_id = %s
            """,
            [(message, candidate.chunk_id) for candidate in batch],
        )
    conn.commit()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create policy_chunk embeddings through an OpenAI-compatible "
            "LiteLLM gateway."
        )
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
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="concurrent gateway requests (default: 1; use a small value)",
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
    if args.workers <= 0:
        parser.error("--workers must be greater than zero")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    with psycopg.connect(DB_DSN) as conn:
        candidates, estimated_tokens = load_candidates(
            conn,
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

        api_key = os.getenv("LITELLM_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "LITELLM_API_KEY is required when --write is used"
            )
        if not EMBEDDING_API_BASE_URL:
            raise RuntimeError(
                "LITELLM_BASE_URL is required when --write is used"
            )
        client = create_embedding_client(api_key, EMBEDDING_API_BASE_URL)

        succeeded = 0
        failed = 0
        actual_tokens = 0
        batches = _batched(candidates, args.batch_size)
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    request_with_retry,
                    client,
                    [candidate.content for candidate in batch],
                    EMBEDDING_MODEL,
                    args.max_attempts,
                    args.base_delay,
                ): (batch_number, batch)
                for batch_number, batch in enumerate(batches, start=1)
            }
            for future in as_completed(futures):
                batch_number, batch = futures[future]
                try:
                    vectors, prompt_tokens = future.result()
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
