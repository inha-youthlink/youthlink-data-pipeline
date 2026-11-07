from __future__ import annotations

import contextlib
import io
import unittest

from etl.search_rag_chunks import format_result, parse_args, search_rag_chunks


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def cursor(self, **kwargs):
        del kwargs
        return self.fake_cursor


class UnifiedRagSearchTests(unittest.TestCase):
    def test_search_uses_one_projection_knn_query_and_all_filters(self):
        cursor = FakeCursor(
            [
                {
                    "retrieval_chunk_id": "R1",
                    "policy_no": "P1",
                    "policy_name": "정책",
                    "source_type": "CRAWLED_WEB",
                    "policy_chunk_id": None,
                    "web_chunk_id": "W1",
                    "document_id": "D1",
                    "source_id": "S1",
                    "source_url": "https://example.test/policy",
                    "chunk_index": 0,
                    "chunk_type": "WEB_TEXT",
                    "content": "검증된 원문",
                    "source_priority": 50,
                    "similarity": 0.9,
                }
            ]
        )
        conn = FakeConnection(cursor)

        rows = search_rag_chunks(
            conn,
            [0.0] * 1023 + [1.0],
            model="test-model",
            limit=5,
            policy_nos=["P1"],
            source_types=["CRAWLED_WEB"],
            chunk_types=["WEB_TEXT"],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(cursor.sql.count("FROM rag_retrieval_chunk"), 1)
        self.assertNotIn("FROM policy_chunk", cursor.sql)
        self.assertNotIn("FROM policy_web_chunk", cursor.sql)
        self.assertIn("p.is_active = TRUE", cursor.sql)
        self.assertIn("retrieval.embedding_model = %s", cursor.sql)
        self.assertIn("retrieval.embedding_encoding_format = %s", cursor.sql)
        self.assertIn("retrieval.policy_no = ANY(%s)", cursor.sql)
        self.assertIn("retrieval.source_type = ANY(%s)", cursor.sql)
        self.assertIn("retrieval.chunk_type = ANY(%s)", cursor.sql)
        self.assertIn(
            "ORDER BY retrieval.embedding <=> %s::vector", cursor.sql
        )
        self.assertNotIn("source_priority DESC", cursor.sql)
        self.assertEqual(
            cursor.params[1:6],
            [
                "test-model",
                "float",
                ["P1"],
                ["CRAWLED_WEB"],
                ["WEB_TEXT"],
            ],
        )
        self.assertEqual(cursor.params[0], cursor.params[-2])
        self.assertEqual(cursor.params[-1], 5)

    def test_search_keeps_provenance_columns_in_result_contract(self):
        cursor = FakeCursor([])
        conn = FakeConnection(cursor)

        search_rag_chunks(
            conn,
            [0.0] * 1024,
            model="test-model",
            limit=1,
        )

        for column in (
            "retrieval.policy_chunk_id",
            "retrieval.web_chunk_id",
            "retrieval.document_id",
            "retrieval.source_id",
            "retrieval.final_url",
            "retrieval.canonical_url",
            "retrieval.source_type",
        ):
            self.assertIn(column, cursor.sql)

    def test_non_positive_limit_is_rejected_by_function(self):
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            search_rag_chunks(
                FakeConnection(FakeCursor()),
                [0.0] * 1024,
                model="test-model",
                limit=0,
            )

    def test_parser_accepts_source_and_chunk_filters(self):
        args = parse_args(
            [
                "청년 월세",
                "--policy-no",
                "P1",
                "--source-type",
                "API",
                "--source-type",
                "CRAWLED_WEB",
                "--chunk-type",
                "SUPPORT",
                "--limit",
                "3",
            ]
        )

        self.assertEqual(args.policy_no, ["P1"])
        self.assertEqual(args.source_type, ["API", "CRAWLED_WEB"])
        self.assertEqual(args.chunk_type, ["SUPPORT"])
        self.assertEqual(args.limit, 3)

    def test_parser_rejects_invalid_source_type(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["질문", "--source-type", "UNVERIFIED"])

    def test_web_result_prints_document_and_url_provenance(self):
        rendered = format_result(
            {
                "policy_no": "P1",
                "policy_name": "정책",
                "source_type": "CRAWLED_WEB",
                "policy_chunk_id": None,
                "web_chunk_id": "W1",
                "document_id": "D1",
                "source_url": "https://example.test/policy",
                "chunk_type": "WEB_TEXT",
                "content": "원문 내용",
                "similarity": 0.9,
            },
            rank=1,
            full_content=False,
        )

        self.assertIn("CRAWLED_WEB", rendered)
        self.assertIn("web_chunk_id=W1", rendered)
        self.assertIn("document_id=D1", rendered)
        self.assertIn("https://example.test/policy", rendered)


if __name__ == "__main__":
    unittest.main()
