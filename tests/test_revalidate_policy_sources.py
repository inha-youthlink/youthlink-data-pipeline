from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from etl.revalidate_policy_sources import (
    RevalidationCandidate,
    extracted_page_from_candidate,
    load_revalidation_candidates,
    parse_args,
)
from etl.source_relevance import VALIDATOR_VERSION


class RecordingCursor:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.executions: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self, rows) -> None:
        self.recording_cursor = RecordingCursor(rows)

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor


class RevalidationSelectionTests(unittest.TestCase):
    def test_selects_pending_old_validator_or_changed_document(self) -> None:
        conn = RecordingConnection([])

        candidates = load_revalidation_candidates(conn, limit=25)

        self.assertEqual(candidates, [])
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("link.validation_status = 'PENDING'", sql)
        self.assertIn("link.validator_version IS DISTINCT FROM %s", sql)
        self.assertIn(
            "link.validated_document_id IS DISTINCT FROM document.document_id",
            sql,
        )
        self.assertIn("document.is_current = TRUE", sql)
        self.assertEqual(params, (VALIDATOR_VERSION, 25))

    def test_rejects_non_positive_limit(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--limit", "0"])

    def test_rebuild_preserves_iframe_retrieval_provenance(self) -> None:
        retrieval = {
            "mode": "NAVER_POST_IFRAME",
            "depth": 1,
            "container_host": "blog.naver.com",
            "container_path": "/example/123",
        }
        candidate = RevalidationCandidate(
            source_id="source-1",
            document_id="document-1",
            final_url="https://blog.naver.com/example/123",
            http_status=200,
            title="정책 제목",
            extracted_text="정책 본문",
            metadata={
                "h1": "정책 제목",
                "headings": ["지원 대상"],
                "retrieval": retrieval,
            },
            policy_nos=("P-1",),
        )

        extracted = extracted_page_from_candidate(candidate)

        self.assertEqual(extracted.retrieval, retrieval)
        self.assertIsNot(extracted.retrieval, retrieval)
        self.assertEqual(extracted.headings, ("지원 대상",))


if __name__ == "__main__":
    unittest.main()
