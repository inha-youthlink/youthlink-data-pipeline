from __future__ import annotations

import unittest
from uuid import uuid4

from etl.generate_policy_web_chunks import (
    CHUNKER_VERSION,
    MAX_CHUNK_CHARS,
    build_web_chunks,
    load_verified_documents,
    sync_web_chunks,
)


class WebChunkRenderingTests(unittest.TestCase):
    def test_builds_deterministic_chunks_from_web_text_only(self) -> None:
        text = "지원 대상과 신청 방법을 안내합니다.\n제출 서류를 확인하세요."

        first = build_web_chunks(title="원문 모집 공고", extracted_text=text)
        second = build_web_chunks(title="원문 모집 공고", extracted_text=text)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].heading, "원문 모집 공고")
        self.assertIn("원문 제목: 원문 모집 공고", first[0].content)
        self.assertNotIn("정책명:", first[0].content)
        self.assertEqual(len(first[0].content_hash), 64)

    def test_splits_long_source_without_exceeding_limit(self) -> None:
        chunks = build_web_chunks(
            title="긴 원문",
            extracted_text="신청 자격과 제출 서류를 안내합니다. " * 250,
        )

        self.assertGreater(len(chunks), 1)
        self.assertEqual(
            [chunk.chunk_index for chunk in chunks],
            list(range(len(chunks))),
        )
        self.assertTrue(
            all(len(chunk.content) <= MAX_CHUNK_CHARS for chunk in chunks)
        )
        self.assertEqual(
            len({chunk.content_hash for chunk in chunks}),
            len(chunks),
        )

    def test_blank_document_produces_no_chunks(self) -> None:
        self.assertEqual(build_web_chunks(title="제목", extracted_text="  \n "), [])

    def test_normalizes_non_breaking_spaces_from_html(self) -> None:
        chunks = build_web_chunks(
            title="모집\u00a0공고",
            extracted_text="2026년\u00a0 3월\u00a0 9일",
        )

        self.assertEqual(len(chunks), 1)
        self.assertNotIn("\u00a0", chunks[0].content)
        self.assertIn("2026년 3월 9일", chunks[0].content)


class RecordingCursor:
    def __init__(self, verified_rows=None) -> None:
        self.verified_rows = verified_rows or []
        self.executions: list[tuple[str, object]] = []
        self.current_rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        if "information_schema.columns" in sql:
            self.current_rows = [
                ("web_chunk_id",),
                ("policy_no",),
                ("document_id",),
                ("content_hash",),
                ("chunker_version",),
                ("embedding",),
            ]
        elif "FROM v_verified_policy_web_document" in sql:
            self.current_rows = self.verified_rows
        elif "FROM policy_web_chunk" in sql:
            self.current_rows = []
        else:
            self.current_rows = []
            self.rowcount = 1

    def fetchall(self):
        return self.current_rows


class RecordingConnection:
    def __init__(self, verified_rows=None) -> None:
        self.recording_cursor = RecordingCursor(verified_rows)

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor


class WebChunkPersistenceTests(unittest.TestCase):
    def test_loader_uses_only_verified_web_document_view(self) -> None:
        conn = RecordingConnection([])

        documents = load_verified_documents(
            conn,
            policy_nos=("P1",),
            limit=3,
        )

        self.assertEqual(documents, [])
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("FROM v_verified_policy_web_document", sql)
        self.assertEqual(params, [["P1"], 3])

    def test_sync_writes_only_separate_web_chunk_table(self) -> None:
        conn = RecordingConnection()
        document_id = uuid4()
        chunk = build_web_chunks(
            title="원문",
            extracted_text="지원 대상과 신청 방법 안내 " * 20,
        )[0]

        stats = sync_web_chunks(conn, {("P1", document_id): (chunk,)})

        sql_text = "\n".join(sql for sql, _ in conn.recording_cursor.executions)
        self.assertEqual(stats["inserted"], 1)
        self.assertIn("INSERT INTO policy_web_chunk", sql_text)
        self.assertNotIn("INSERT INTO policy_chunk", sql_text)
        self.assertNotIn("UPDATE policy_chunk", sql_text)
        self.assertNotIn("UPDATE policy\n", sql_text)
        insert_params = next(
            params
            for sql, params in conn.recording_cursor.executions
            if "INSERT INTO policy_web_chunk" in sql
        )
        self.assertEqual(insert_params[-1], CHUNKER_VERSION)


if __name__ == "__main__":
    unittest.main()
