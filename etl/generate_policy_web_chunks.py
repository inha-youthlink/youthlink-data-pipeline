from __future__ import annotations

import argparse
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
CHUNKER_VERSION = "web-v1"
CHUNK_TYPE = "WEB_TEXT"
MAX_CHUNK_CHARS = 1600
MAX_HEADING_CHARS = 300


@dataclass(frozen=True)
class VerifiedWebDocument:
    policy_no: str
    document_id: Any
    title: str
    extracted_text: str


@dataclass(frozen=True)
class WebChunk:
    chunk_index: int
    chunk_type: str
    heading: str | None
    content: str
    content_hash: str


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = text.replace("\ufeff", "").replace("\u200b", "")
    lines: list[str] = []
    previous_blank = False
    for raw_line in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
        is_blank = not line
        if is_blank and previous_blank:
            continue
        lines.append(line)
        previous_blank = is_blank
    return "\n".join(lines).strip()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _split_long_segment(segment: str, limit: int) -> list[str]:
    pieces: list[str] = []
    remaining = segment.strip()
    minimum_boundary = max(1, limit // 2)
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        boundary = -1
        for separator in ("다. ", ". ", "? ", "! ", "; ", " "):
            candidate = window.rfind(separator)
            if candidate >= minimum_boundary:
                boundary = candidate + len(separator.rstrip())
                break
        if boundary < minimum_boundary:
            boundary = limit
        piece = remaining[:boundary].strip()
        if piece:
            pieces.append(piece)
        remaining = remaining[boundary:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def build_web_chunks(
    *,
    title: Any,
    extracted_text: Any,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[WebChunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")

    normalized_title = normalize_text(title)[:MAX_HEADING_CHARS]
    normalized_body = normalize_text(extracted_text)
    if not normalized_body:
        return []

    prefix = f"원문 제목: {normalized_title}\n" if normalized_title else ""
    body_limit = max_chars - len(prefix)
    if body_limit < 100:
        raise ValueError("max_chars is too small for the source heading")

    segments: list[str] = []
    for line in normalized_body.splitlines():
        if line:
            segments.extend(_split_long_segment(line, body_limit))

    bodies: list[str] = []
    current: list[str] = []
    current_length = 0
    for segment in segments:
        separator_length = 1 if current else 0
        if current and current_length + separator_length + len(segment) > body_limit:
            bodies.append("\n".join(current))
            current = []
            current_length = 0
            separator_length = 0
        current.append(segment)
        current_length += separator_length + len(segment)
    if current:
        bodies.append("\n".join(current))

    chunks: list[WebChunk] = []
    seen_hashes: set[str] = set()
    for body in bodies:
        content = normalize_text(prefix + body)
        digest = content_hash(content)
        if not content or digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        chunks.append(
            WebChunk(
                chunk_index=len(chunks),
                chunk_type=CHUNK_TYPE,
                heading=normalized_title or None,
                content=content,
                content_hash=digest,
            )
        )
    return chunks


def load_verified_documents(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str] = (),
    limit: int | None = None,
) -> list[VerifiedWebDocument]:
    conditions: list[str] = []
    params: list[Any] = []
    if policy_nos:
        conditions.append("policy_no = ANY(%s)")
        params.append(list(policy_nos))

    sql = """
        SELECT policy_no, document_id, COALESCE(title, '') AS title,
               extracted_text
        FROM v_verified_policy_web_document
    """
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY policy_no, document_id"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [VerifiedWebDocument(**row) for row in cur.fetchall()]


def generate_web_chunks(
    conn: psycopg.Connection[Any],
    *,
    policy_nos: Sequence[str] = (),
    limit: int | None = None,
) -> dict[tuple[str, Any], list[WebChunk]]:
    documents = load_verified_documents(
        conn,
        policy_nos=policy_nos,
        limit=limit,
    )
    return {
        (document.policy_no, document.document_id): build_web_chunks(
            title=document.title,
            extracted_text=document.extracted_text,
        )
        for document in documents
    }


def verify_web_chunk_schema(conn: psycopg.Connection[Any]) -> None:
    required = {
        "web_chunk_id",
        "policy_no",
        "document_id",
        "content_hash",
        "chunker_version",
        "embedding",
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'policy_web_chunk'
            """
        )
        existing = {row[0] for row in cur.fetchall()}
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            "policy_web_chunk schema is not ready; apply "
            "db/migrations/011_add_separate_policy_web_chunks.sql first. "
            f"Missing columns: {', '.join(missing)}"
        )


def sync_web_chunks(
    conn: psycopg.Connection[Any],
    generated: Mapping[tuple[str, Any], Sequence[WebChunk]],
) -> dict[str, int]:
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    if not generated:
        return stats

    verify_web_chunk_schema(conn)
    policy_nos = sorted({key[0] for key in generated})
    document_ids = list({key[1] for key in generated})

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT web_chunk_id, policy_no, document_id, chunk_index,
                   chunk_type, heading, content, content_hash, chunker_version
            FROM policy_web_chunk
            WHERE policy_no = ANY(%s)
              AND document_id = ANY(%s)
            """,
            (policy_nos, document_ids),
        )
        existing_rows = [
            row
            for row in cur.fetchall()
            if (row["policy_no"], row["document_id"]) in generated
        ]

        existing_by_key = {
            (row["policy_no"], row["document_id"], row["chunk_index"]): row
            for row in existing_rows
        }
        desired_by_key = {
            (policy_no, document_id, chunk.chunk_index): chunk
            for (policy_no, document_id), chunks in generated.items()
            for chunk in chunks
        }

        delete_ids = [
            row["web_chunk_id"]
            for row in existing_rows
            if (
                row["policy_no"],
                row["document_id"],
                row["chunk_index"],
            )
            not in desired_by_key
        ]
        if delete_ids:
            cur.execute(
                "DELETE FROM policy_web_chunk WHERE web_chunk_id = ANY(%s)",
                (delete_ids,),
            )
            stats["deleted"] += cur.rowcount

        for (policy_no, document_id), chunks in generated.items():
            for chunk in chunks:
                key = (policy_no, document_id, chunk.chunk_index)
                old = existing_by_key.get(key)
                if (
                    old is not None
                    and old["chunk_type"] == chunk.chunk_type
                    and old["heading"] == chunk.heading
                    and old["content"] == chunk.content
                    and old["content_hash"] == chunk.content_hash
                    and old["chunker_version"] == CHUNKER_VERSION
                ):
                    stats["unchanged"] += 1
                    continue

                if old is None:
                    cur.execute(
                        """
                        INSERT INTO policy_web_chunk (
                            policy_no, document_id, chunk_index, chunk_type,
                            heading, content, content_hash, chunker_version
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            policy_no,
                            document_id,
                            chunk.chunk_index,
                            chunk.chunk_type,
                            chunk.heading,
                            chunk.content,
                            chunk.content_hash,
                            CHUNKER_VERSION,
                        ),
                    )
                    stats["inserted"] += 1
                else:
                    cur.execute(
                        """
                        UPDATE policy_web_chunk
                        SET chunk_type = %s,
                            heading = %s,
                            content = %s,
                            content_hash = %s,
                            chunker_version = %s,
                            token_count = NULL,
                            embedding = NULL,
                            embedding_model = NULL,
                            embedded_at = NULL,
                            embedding_error = NULL,
                            updated_at = NOW()
                        WHERE web_chunk_id = %s
                        """,
                        (
                            chunk.chunk_type,
                            chunk.heading,
                            chunk.content,
                            chunk.content_hash,
                            CHUNKER_VERSION,
                            old["web_chunk_id"],
                        ),
                    )
                    stats["updated"] += 1
    return stats


def print_preview(
    generated: Mapping[tuple[str, Any], Sequence[WebChunk]],
    sample_size: int,
) -> None:
    total = sum(len(chunks) for chunks in generated.values())
    print(
        f"generated {total} separate web chunks for "
        f"{len(generated)} verified policy documents (dry-run)"
    )
    for (policy_no, document_id), chunks in list(generated.items())[:sample_size]:
        print(f"\n[{policy_no} document={document_id}]")
        for chunk in chunks:
            print(f"\n--- {chunk.chunk_index} {chunk.chunk_type} ---")
            print(chunk.content)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate independent RAG fallback chunks from verified crawled "
            "documents. API policy and policy_chunk rows are never modified."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write policy_web_chunk rows (default: dry-run)",
    )
    parser.add_argument(
        "--policy-no",
        action="append",
        default=[],
        help="process only this policy number; may be repeated",
    )
    parser.add_argument("--limit", type=int, help="maximum verified documents")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=2,
        help="documents printed during dry-run (default: 2)",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.sample_size < 0:
        parser.error("--sample-size must not be negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    limit = args.limit
    if not args.write and limit is None and not args.policy_no:
        limit = 5

    with psycopg.connect(DB_DSN) as conn:
        generated = generate_web_chunks(
            conn,
            policy_nos=args.policy_no,
            limit=limit,
        )
        if not args.write:
            print_preview(generated, args.sample_size)
            return 0

        stats = sync_web_chunks(conn, generated)
        conn.commit()

    total = sum(len(chunks) for chunks in generated.values())
    print(
        f"synced {total} separate web chunks for {len(generated)} documents: "
        f"inserted={stats['inserted']}, updated={stats['updated']}, "
        f"unchanged={stats['unchanged']}, deleted={stats['deleted']}"
    )
    print("API-owned policy and policy_chunk rows were not modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
