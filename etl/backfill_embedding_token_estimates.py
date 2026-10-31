from __future__ import annotations

import argparse
import os
from typing import Any, Mapping, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

try:
    from .embedding_utils import estimate_token_count
except ImportError:  # Direct execution from repository root
    from embedding_utils import estimate_token_count


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)

TABLES = (
    ("policy_chunk", "chunk_id"),
    ("policy_web_chunk", "web_chunk_id"),
)


def calculate_updates(
    rows: Sequence[Mapping[str, Any]],
    id_column: str,
) -> list[tuple[int, Any]]:
    updates: list[tuple[int, Any]] = []
    for row in rows:
        estimate = estimate_token_count(row["content"])
        if row["token_count"] != estimate:
            updates.append((estimate, row[id_column]))
    return updates


def load_rows(
    conn: psycopg.Connection[Any],
    table_name: str,
    id_column: str,
) -> list[dict[str, Any]]:
    query = sql.SQL(
        "SELECT {id_column}, content, token_count "
        "FROM {table_name} "
        "WHERE embedding IS NOT NULL "
        "ORDER BY {id_column}"
    ).format(
        id_column=sql.Identifier(id_column),
        table_name=sql.Identifier(table_name),
    )
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query)
        return list(cursor.fetchall())


def store_updates(
    conn: psycopg.Connection[Any],
    table_name: str,
    id_column: str,
    updates: Sequence[tuple[int, Any]],
) -> None:
    if not updates:
        return
    statement = sql.SQL(
        "UPDATE {table_name} SET token_count = %s WHERE {id_column} = %s"
    ).format(
        table_name=sql.Identifier(table_name),
        id_column=sql.Identifier(id_column),
    )
    with conn.cursor() as cursor:
        cursor.executemany(statement, updates)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recalculate local token_count estimates for existing policy and "
            "verified-web embeddings without calling an external API."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="persist changed estimates (default: plan only)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    total_rows = 0
    total_updates = 0

    with psycopg.connect(DB_DSN) as conn:
        for table_name, id_column in TABLES:
            rows = load_rows(conn, table_name, id_column)
            updates = calculate_updates(rows, id_column)
            total_rows += len(rows)
            total_updates += len(updates)
            print(
                f"{table_name}: embedded={len(rows)}, "
                f"estimates_to_update={len(updates)}"
            )
            if args.write:
                store_updates(conn, table_name, id_column, updates)

        if args.write:
            conn.commit()
            print(
                f"token estimate backfill complete: "
                f"scanned={total_rows}, updated={total_updates}"
            )
        else:
            conn.rollback()
            print(
                f"plan only: scanned={total_rows}, "
                f"estimates_to_update={total_updates}; add --write to persist"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
