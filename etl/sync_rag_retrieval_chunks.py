from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv

try:
    from .embedding_utils import DEFAULT_EMBEDDING_ENCODING_FORMAT
except ImportError:  # Direct execution
    from embedding_utils import DEFAULT_EMBEDDING_ENCODING_FORMAT


load_dotenv()

DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
LOCK_NAME = "youthlink:rag-retrieval-publish"
API_SOURCE_TYPE = "API"
WEB_SOURCE_TYPE = "CRAWLED_WEB"
API_SOURCE_PRIORITY = 100
WEB_SOURCE_PRIORITY = 50
DESIRED_TABLE = "desired_rag_retrieval_chunk"


@dataclass(frozen=True)
class SyncStats:
    api_total: int
    api_retrievable: int
    api_pending: int
    api_errors: int
    web_total: int
    web_retrievable: int
    web_pending: int
    web_errors: int
    inserted: int
    updated: int
    unchanged: int
    deleted: int

    @property
    def desired_total(self) -> int:
        return self.api_retrievable + self.web_retrievable


REQUIRED_COLUMNS = {
    "retrieval_chunk_id",
    "policy_no",
    "source_type",
    "policy_chunk_id",
    "web_chunk_id",
    "document_id",
    "source_id",
    "canonical_url",
    "final_url",
    "source_fetched_at",
    "chunk_index",
    "chunk_type",
    "heading",
    "content",
    "content_hash",
    "chunker_version",
    "embedding",
    "embedding_model",
    "embedding_encoding_format",
    "token_count",
    "embedded_at",
    "source_updated_at",
    "source_priority",
    "created_at",
    "updated_at",
}

COPY_COLUMNS = (
    "policy_no",
    "source_type",
    "policy_chunk_id",
    "web_chunk_id",
    "document_id",
    "source_id",
    "canonical_url",
    "final_url",
    "source_fetched_at",
    "chunk_index",
    "chunk_type",
    "heading",
    "content",
    "content_hash",
    "chunker_version",
    "embedding",
    "embedding_model",
    "embedding_encoding_format",
    "token_count",
    "embedded_at",
    "source_updated_at",
    "source_priority",
)

# Comparing 14k 1024-dimensional vectors and full text values on every dry-run
# is unnecessarily expensive. content_hash identifies the text, while the
# model and embedded_at timestamp identify the stored vector generation. The
# full values are still copied whenever one of these lineage fields changes.
COMPARE_COLUMNS = tuple(
    column for column in COPY_COLUMNS if column not in {"content", "embedding"}
)


def normalize_policy_nos(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def verify_schema(conn: psycopg.Connection[Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'rag_retrieval_chunk'
            """
        )
        existing = {row[0] for row in cur.fetchall()}
    missing = sorted(REQUIRED_COLUMNS - existing)
    if missing:
        raise RuntimeError(
            "rag_retrieval_chunk schema is not ready; apply "
            "db/migrations through 018_track_float_embedding_format.sql first. "
            f"Missing columns: {', '.join(missing)}"
        )


def acquire_publish_lock(conn: psycopg.Connection[Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            (LOCK_NAME,),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        raise RuntimeError(
            "another rag retrieval publish is already running; try again later"
        )


def _scope_sql(alias: str, policy_nos: Sequence[str]) -> tuple[str, list[Any]]:
    if not policy_nos:
        return "", []
    return f" AND {alias}.policy_no = ANY(%s)", [list(policy_nos)]


def create_desired_snapshot(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str] = (),
    include_payload: bool = True,
) -> None:
    api_scope, api_params = _scope_sql("pc", policy_nos)
    web_scope, web_params = _scope_sql("verified", policy_nos)
    api_content = "pc.content" if include_payload else "NULL::TEXT"
    api_embedding = "pc.embedding" if include_payload else "NULL::VECTOR(1024)"
    web_content = "verified.content" if include_payload else "NULL::TEXT"
    web_embedding = (
        "verified.embedding" if include_payload else "NULL::VECTOR(1024)"
    )
    sql = f"""
        CREATE TEMP TABLE {DESIRED_TABLE} ON COMMIT DROP AS
        SELECT
            pc.policy_no,
            '{API_SOURCE_TYPE}'::VARCHAR(30) AS source_type,
            pc.chunk_id AS policy_chunk_id,
            NULL::UUID AS web_chunk_id,
            NULL::UUID AS document_id,
            NULL::UUID AS source_id,
            NULL::TEXT AS canonical_url,
            NULL::TEXT AS final_url,
            NULL::TIMESTAMPTZ AS source_fetched_at,
            pc.chunk_index,
            pc.chunk_type,
            NULL::TEXT AS heading,
            {api_content} AS content,
            pc.content_hash,
            pc.chunker_version,
            {api_embedding} AS embedding,
            pc.embedding_model,
            pc.embedding_encoding_format,
            pc.token_count,
            pc.embedded_at,
            pc.updated_at AS source_updated_at,
            {API_SOURCE_PRIORITY}::SMALLINT AS source_priority
        FROM policy_chunk AS pc
        JOIN policy AS policy_row
          ON policy_row.policy_no = pc.policy_no
        WHERE policy_row.is_active = TRUE
          AND pc.embedding IS NOT NULL
          AND pc.embedding_model IS NOT NULL
          AND BTRIM(pc.embedding_model) <> ''
          AND pc.embedding_encoding_format = '{DEFAULT_EMBEDDING_ENCODING_FORMAT}'
          AND pc.embedding_error IS NULL
          {api_scope}

        UNION ALL

        SELECT
            verified.policy_no,
            '{WEB_SOURCE_TYPE}'::VARCHAR(30) AS source_type,
            NULL::UUID AS policy_chunk_id,
            verified.web_chunk_id,
            verified.document_id,
            verified.source_id,
            verified.canonical_url,
            verified.final_url,
            verified.fetched_at AS source_fetched_at,
            verified.chunk_index,
            verified.chunk_type,
            verified.heading,
            {web_content} AS content,
            verified.content_hash,
            verified.chunker_version,
            {web_embedding} AS embedding,
            verified.embedding_model,
            verified.embedding_encoding_format,
            verified.token_count,
            verified.embedded_at,
            verified.updated_at AS source_updated_at,
            {WEB_SOURCE_PRIORITY}::SMALLINT AS source_priority
        FROM v_verified_policy_web_chunk AS verified
        JOIN policy_web_chunk AS web_chunk
          ON web_chunk.web_chunk_id = verified.web_chunk_id
        WHERE verified.embedding IS NOT NULL
          AND verified.embedding_model IS NOT NULL
          AND BTRIM(verified.embedding_model) <> ''
          AND verified.embedding_encoding_format = '{DEFAULT_EMBEDDING_ENCODING_FORMAT}'
          AND web_chunk.embedding_error IS NULL
          {web_scope}
    """
    with conn.cursor() as cur:
        cur.execute(sql, [*api_params, *web_params])
        cur.execute(
            f"""
            CREATE UNIQUE INDEX desired_rag_retrieval_policy_chunk
                ON {DESIRED_TABLE} (policy_chunk_id)
                WHERE policy_chunk_id IS NOT NULL
            """
        )
        cur.execute(
            f"""
            CREATE UNIQUE INDEX desired_rag_retrieval_web_chunk
                ON {DESIRED_TABLE} (web_chunk_id)
                WHERE web_chunk_id IS NOT NULL
            """
        )


def _load_source_counts(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str],
) -> tuple[int, int, int, int, int, int, int, int]:
    api_scope, api_params = _scope_sql("pc", policy_nos)
    web_scope, web_params = _scope_sql("verified", policy_nos)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*),
                COUNT(*) FILTER (
                    WHERE pc.embedding IS NOT NULL
                      AND pc.embedding_model IS NOT NULL
                      AND BTRIM(pc.embedding_model) <> ''
                      AND pc.embedding_encoding_format = '{DEFAULT_EMBEDDING_ENCODING_FORMAT}'
                      AND pc.embedding_error IS NULL
                ),
                COUNT(*) FILTER (
                    WHERE pc.embedding_error IS NULL
                      AND NOT (
                          pc.embedding IS NOT NULL
                          AND pc.embedding_model IS NOT NULL
                          AND BTRIM(pc.embedding_model) <> ''
                          AND pc.embedding_encoding_format = '{DEFAULT_EMBEDDING_ENCODING_FORMAT}'
                      )
                ),
                COUNT(*) FILTER (WHERE pc.embedding_error IS NOT NULL)
            FROM policy_chunk AS pc
            JOIN policy AS policy_row
              ON policy_row.policy_no = pc.policy_no
            WHERE policy_row.is_active = TRUE
              {api_scope}
            """,
            api_params,
        )
        api_row = cur.fetchone()
        if api_row is None:
            raise RuntimeError("failed to count API policy chunks")

        cur.execute(
            f"""
            SELECT
                COUNT(*),
                COUNT(*) FILTER (
                    WHERE verified.embedding IS NOT NULL
                      AND verified.embedding_model IS NOT NULL
                      AND BTRIM(verified.embedding_model) <> ''
                      AND verified.embedding_encoding_format = '{DEFAULT_EMBEDDING_ENCODING_FORMAT}'
                      AND web_chunk.embedding_error IS NULL
                ),
                COUNT(*) FILTER (
                    WHERE web_chunk.embedding_error IS NULL
                      AND NOT (
                          verified.embedding IS NOT NULL
                          AND verified.embedding_model IS NOT NULL
                          AND BTRIM(verified.embedding_model) <> ''
                          AND verified.embedding_encoding_format = '{DEFAULT_EMBEDDING_ENCODING_FORMAT}'
                      )
                ),
                COUNT(*) FILTER (
                    WHERE web_chunk.embedding_error IS NOT NULL
                )
            FROM v_verified_policy_web_chunk AS verified
            JOIN policy_web_chunk AS web_chunk
              ON web_chunk.web_chunk_id = verified.web_chunk_id
            WHERE TRUE
              {web_scope}
            """,
            web_params,
        )
        web_row = cur.fetchone()
        if web_row is None:
            raise RuntimeError("failed to count verified web chunks")
    return (*map(int, api_row), *map(int, web_row))


def _changed_predicate(target: str, desired: str) -> str:
    comparable = COMPARE_COLUMNS
    left = ", ".join(f"{target}.{column}" for column in comparable)
    right = ", ".join(f"{desired}.{column}" for column in comparable)
    return f"ROW({left}) IS DISTINCT FROM ROW({right})"


def _load_delta_counts(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str],
) -> tuple[int, int, int, int]:
    changed = _changed_predicate("target", "desired")
    target_scope, target_params = _scope_sql("target", policy_nos)
    inserted = 0
    updated = 0
    unchanged = 0
    with conn.cursor() as cur:
        # Separate origin-specific joins let PostgreSQL use the two UNIQUE
        # origin indexes. A single OR join degrades into a costly broad scan
        # once the projection contains the full API corpus.
        for source_type, origin_column in (
            (API_SOURCE_TYPE, "policy_chunk_id"),
            (WEB_SOURCE_TYPE, "web_chunk_id"),
        ):
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE target.retrieval_chunk_id IS NULL),
                    COUNT(*) FILTER (
                        WHERE target.retrieval_chunk_id IS NOT NULL
                          AND {changed}
                    ),
                    COUNT(*) FILTER (
                        WHERE target.retrieval_chunk_id IS NOT NULL
                          AND NOT ({changed})
                    )
                FROM {DESIRED_TABLE} AS desired
                LEFT JOIN rag_retrieval_chunk AS target
                  ON target.{origin_column} = desired.{origin_column}
                WHERE desired.source_type = %s
                """,
                (source_type,),
            )
            delta_row = cur.fetchone()
            if delta_row is None:
                raise RuntimeError(
                    "failed to calculate retrieval publish changes"
                )
            inserted += int(delta_row[0])
            updated += int(delta_row[1])
            unchanged += int(delta_row[2])

        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM rag_retrieval_chunk AS target
            WHERE TRUE
              {target_scope}
              AND (
                  (
                      target.source_type = '{API_SOURCE_TYPE}'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM {DESIRED_TABLE} AS desired
                          WHERE desired.source_type = '{API_SOURCE_TYPE}'
                            AND desired.policy_chunk_id = target.policy_chunk_id
                      )
                  )
                  OR
                  (
                      target.source_type = '{WEB_SOURCE_TYPE}'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM {DESIRED_TABLE} AS desired
                          WHERE desired.source_type = '{WEB_SOURCE_TYPE}'
                            AND desired.web_chunk_id = target.web_chunk_id
                      )
                  )
              )
            """,
            target_params,
        )
        delete_row = cur.fetchone()
        if delete_row is None:
            raise RuntimeError("failed to calculate stale retrieval chunks")
    return inserted, updated, unchanged, int(delete_row[0])


def plan_retrieval_sync(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str] = (),
) -> SyncStats:
    normalized = normalize_policy_nos(policy_nos)
    verify_schema(conn)
    create_desired_snapshot(
        conn,
        policy_nos=normalized,
        include_payload=False,
    )
    return SyncStats(
        *_load_source_counts(conn, policy_nos=normalized),
        *_load_delta_counts(conn, policy_nos=normalized),
    )


def _upsert_source(
    conn: psycopg.Connection[Any],
    *,
    source_type: str,
    conflict_column: str,
) -> int:
    columns = ", ".join(COPY_COLUMNS)
    assignments = ",\n".join(
        f"                {column} = EXCLUDED.{column}"
        for column in COPY_COLUMNS
        if column != conflict_column
    )
    changed = _changed_predicate("rag_retrieval_chunk", "EXCLUDED")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO rag_retrieval_chunk ({columns})
            SELECT {columns}
            FROM {DESIRED_TABLE}
            WHERE source_type = %s
            ON CONFLICT ({conflict_column}) DO UPDATE
            SET
{assignments},
                updated_at = NOW()
            WHERE {changed}
            """,
            (source_type,),
        )
        return cur.rowcount


def _delete_stale(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str],
) -> int:
    target_scope, target_params = _scope_sql("target", policy_nos)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM rag_retrieval_chunk AS target
            WHERE TRUE
              {target_scope}
              AND (
                  (
                      target.source_type = '{API_SOURCE_TYPE}'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM {DESIRED_TABLE} AS desired
                          WHERE desired.source_type = '{API_SOURCE_TYPE}'
                            AND desired.policy_chunk_id = target.policy_chunk_id
                      )
                  )
                  OR
                  (
                      target.source_type = '{WEB_SOURCE_TYPE}'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM {DESIRED_TABLE} AS desired
                          WHERE desired.source_type = '{WEB_SOURCE_TYPE}'
                            AND desired.web_chunk_id = target.web_chunk_id
                      )
                  )
              )
            """,
            target_params,
        )
        return cur.rowcount


def sync_retrieval_chunks(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str] = (),
) -> SyncStats:
    normalized = normalize_policy_nos(policy_nos)
    verify_schema(conn)
    acquire_publish_lock(conn)
    create_desired_snapshot(conn, policy_nos=normalized)
    stats = SyncStats(
        *_load_source_counts(conn, policy_nos=normalized),
        *_load_delta_counts(conn, policy_nos=normalized),
    )
    affected = _upsert_source(
        conn,
        source_type=API_SOURCE_TYPE,
        conflict_column="policy_chunk_id",
    )
    affected += _upsert_source(
        conn,
        source_type=WEB_SOURCE_TYPE,
        conflict_column="web_chunk_id",
    )
    deleted = _delete_stale(conn, policy_nos=normalized)
    if affected != stats.inserted + stats.updated or deleted != stats.deleted:
        raise RuntimeError(
            "retrieval publish changed while applying its source snapshot; "
            "the transaction will be rolled back"
        )
    return stats


def print_stats(stats: SyncStats, *, policy_nos: Sequence[str]) -> None:
    scope = ",".join(policy_nos) if policy_nos else "all active policies"
    print(f"rag retrieval publish plan: scope={scope}")
    print(
        "api chunks: "
        f"total={stats.api_total}, retrievable={stats.api_retrievable}, "
        f"pending={stats.api_pending}, errors={stats.api_errors}"
    )
    print(
        "verified web chunks: "
        f"total={stats.web_total}, retrievable={stats.web_retrievable}, "
        f"pending={stats.web_pending}, errors={stats.web_errors}"
    )
    print(
        f"desired={stats.desired_total}, inserted={stats.inserted}, "
        f"updated={stats.updated}, unchanged={stats.unchanged}, "
        f"deleted={stats.deleted}"
    )


def _nonblank_policy_no(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("policy number must not be blank")
    return normalized


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Publish active API chunks and current verified web chunks into "
            "one pgvector retrieval index without modifying either source."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="publish rag_retrieval_chunk rows (default: dry-run)",
    )
    parser.add_argument(
        "--policy-no",
        action="append",
        type=_nonblank_policy_no,
        default=[],
        help=(
            "publish only this policy; may be repeated. Stale deletion is "
            "restricted to the same policy scope."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    policy_nos = normalize_policy_nos(args.policy_no)
    with psycopg.connect(DB_DSN) as conn:
        if args.write:
            stats = sync_retrieval_chunks(conn, policy_nos=policy_nos)
            conn.commit()
        else:
            stats = plan_retrieval_sync(conn, policy_nos=policy_nos)
            conn.rollback()

    print_stats(stats, policy_nos=policy_nos)
    if args.write:
        print("rag retrieval chunks published atomically")
    else:
        print("dry-run only; add --write to publish the retrieval index")
    print("policy_chunk and policy_web_chunk source rows were not modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
