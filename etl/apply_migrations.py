from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import time
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv


load_dotenv()

DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_DIR = REPO_ROOT / "db" / "migrations"

# Shared legacy 008/009 chat files are intentionally not renamed or replayed.
# Migration 015 converges every supported chat state; the canonical policy/web
# files are listed explicitly so duplicate numeric prefixes are unambiguous.
MIGRATIONS = (
    "001_add_region_master.sql",
    "002_change_region_code_mapping.sql",
    "003_add_policy_region_resolved_view.sql",
    "004_retire_region_code_alias.sql",
    "005_prepare_policy_chunk_pipeline.sql",
    "006_add_policy_application_period.sql",
    "007_complete_incheon_scope_and_add_scope_views.sql",
    "008_add_policy_sync_tracking.sql",
    "009_add_policy_web_sources.sql",
    "010_bind_web_validation_to_document.sql",
    "011_add_separate_policy_web_chunks.sql",
    "012_enforce_validated_document_source.sql",
    "013_change_embedding_dimension_to_1024.sql",
    "014_resolve_daejeon_parent_region.sql",
    "015_converge_chat_schema.sql",
    "016_add_rag_retrieval_chunks.sql",
    "017_add_policy_web_ai_review.sql",
    "018_track_float_embedding_format.sql",
    "019_add_pipeline_operations.sql",
)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_history_table(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            migration_name VARCHAR(200) PRIMARY KEY,
            checksum_sha256 VARCHAR(64) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            execution_ms INTEGER,
            CONSTRAINT chk_schema_migration_checksum
                CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT chk_schema_migration_execution
                CHECK (execution_ms IS NULL OR execution_ms >= 0)
        )
        """
    )
    conn.commit()


def load_history(conn: psycopg.Connection[Any]) -> dict[str, str]:
    return {
        str(name): str(digest)
        for name, digest in conn.execute(
            "SELECT migration_name, checksum_sha256 FROM schema_migration"
        ).fetchall()
    }


def verify_history(history: dict[str, str]) -> None:
    for name, recorded in history.items():
        if name not in MIGRATIONS:
            continue
        actual = checksum(MIGRATION_DIR / name)
        if actual != recorded:
            raise RuntimeError(
                f"applied migration checksum changed: {name}; "
                "restore the original file and add a new migration"
            )


def baseline(
    conn: psycopg.Connection[Any],
    *,
    through: str,
) -> int:
    last = MIGRATIONS.index(through)
    inserted = 0
    for name in MIGRATIONS[: last + 1]:
        result = conn.execute(
            """
            INSERT INTO schema_migration (
                migration_name, checksum_sha256, execution_ms
            ) VALUES (%s, %s, NULL)
            ON CONFLICT (migration_name) DO NOTHING
            """,
            (name, checksum(MIGRATION_DIR / name)),
        )
        inserted += result.rowcount
    conn.commit()
    return inserted


def apply_pending(
    conn: psycopg.Connection[Any],
    history: dict[str, str],
) -> int:
    if not history:
        raise RuntimeError(
            "migration history is empty; verify the existing schema and use "
            "--baseline-through before applying future migrations"
        )
    applied = 0
    conn.commit()
    conn.autocommit = True
    try:
        for name in MIGRATIONS:
            if name in history:
                continue
            path = MIGRATION_DIR / name
            started = time.monotonic()
            conn.execute(path.read_text(encoding="utf-8"))
            elapsed_ms = round((time.monotonic() - started) * 1000)
            conn.execute(
                """
                INSERT INTO schema_migration (
                    migration_name, checksum_sha256, execution_ms
                ) VALUES (%s, %s, %s)
                """,
                (name, checksum(path), elapsed_ms),
            )
            print(f"applied {name} ({elapsed_ms} ms)")
            applied += 1
    finally:
        conn.autocommit = False
    return applied


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply YouthLink migrations in the explicit canonical order."
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--baseline-through", choices=MIGRATIONS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    with psycopg.connect(DB_DSN) as conn:
        ensure_history_table(conn)
        history = load_history(conn)
        verify_history(history)
        if args.baseline_through:
            count = baseline(conn, through=args.baseline_through)
            print(f"migration baseline recorded: {count} new entries")
            return 0
        if args.status:
            for name in MIGRATIONS:
                print(("APPLIED" if name in history else "PENDING") + f" {name}")
            return 0
        count = apply_pending(conn, history)
        print(f"migration apply complete: {count} applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
