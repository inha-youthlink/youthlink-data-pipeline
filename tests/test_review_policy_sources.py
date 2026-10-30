from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from uuid import UUID, uuid4

from etl.review_policy_sources import (
    apply_manual_review,
    load_locked_review_target,
    load_review_candidates,
    parse_args,
)


class RecordingCursor:
    def __init__(self, responses=None, update_rows=None) -> None:
        self.responses = list(responses or [])
        self.update_rows = list(update_rows or [])
        self.executions: list[tuple[str, object]] = []
        self.current_rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        if sql.lstrip().upper().startswith("UPDATE"):
            self.current_rows = self.update_rows
            self.rowcount = len(self.current_rows)
        else:
            self.current_rows = self.responses.pop(0) if self.responses else []
            self.rowcount = len(self.current_rows)

    def fetchall(self):
        return self.current_rows

    def fetchone(self):
        return self.current_rows[0] if self.current_rows else None


class RecordingConnection:
    def __init__(self, responses=None, update_rows=None) -> None:
        self.recording_cursor = RecordingCursor(responses, update_rows)

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor


def target_row(
    document_id: UUID,
    *,
    relation_type: str = "REFERENCE_1",
    status: str = "REVIEW_REQUIRED",
    reviewed_at=None,
):
    return {
        "relation_type": relation_type,
        "validation_status": status,
        "reviewed_at": reviewed_at,
        "validated_document_id": document_id,
    }


def locked_target_responses(document_id: UUID, rows):
    return [
        [{"policy_no": "P1"}],
        [{"source_id": uuid4()}],
        [{"document_id": document_id}],
        rows,
    ]


class ArgumentTests(unittest.TestCase):
    def test_listing_is_the_default_and_never_writes(self) -> None:
        args = parse_args([])

        self.assertFalse(args.write)
        self.assertIsNone(args.decision)

    def test_manual_action_requires_exact_expected_document(self) -> None:
        document_id = str(uuid4())

        args = parse_args(
            [
                "--policy-no",
                "P1",
                "--source-id",
                str(uuid4()),
                "--expected-document-id",
                document_id,
                "--decision",
                "VERIFIED",
                "--reviewed-by",
                "reviewer",
                "--reason",
                "정책명과 공고 내용이 일치함",
            ]
        )

        self.assertEqual(str(args.expected_document_id), document_id)
        self.assertFalse(args.write)

    def test_rejects_incomplete_manual_action(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--policy-no", "P1", "--decision", "VERIFIED"])

    def test_write_requires_complete_manual_action(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--write"])


class SelectionTests(unittest.TestCase):
    def test_candidates_are_active_current_unreviewed_review_required_only(self):
        conn = RecordingConnection([[]])

        candidates = load_review_candidates(conn, limit=10)

        self.assertEqual(candidates, [])
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("policy_row.is_active = TRUE", sql)
        self.assertIn("document.is_current = TRUE", sql)
        self.assertIn("link.is_active = TRUE", sql)
        self.assertIn("link.validation_status = 'REVIEW_REQUIRED'", sql)
        self.assertIn("link.reviewed_at IS NULL", sql)
        self.assertEqual(params, (10,))

    def test_target_requires_exact_validated_document_version(self) -> None:
        expected_document_id = uuid4()
        other_document_id = uuid4()
        conn = RecordingConnection(
            locked_target_responses(
                expected_document_id,
                [target_row(other_document_id)],
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "version that was validated"):
            load_locked_review_target(
                conn,
                policy_no="P1",
                source_id=str(uuid4()),
                document_id=str(expected_document_id),
            )

    def test_target_rejects_non_current_document(self) -> None:
        document_id = uuid4()
        conn = RecordingConnection(
            [
                [{"policy_no": "P1"}],
                [{"source_id": uuid4()}],
                [],
            ],
        )

        with self.assertRaisesRegex(RuntimeError, "no longer current"):
            load_locked_review_target(
                conn,
                policy_no="P1",
                source_id=str(uuid4()),
                document_id=str(document_id),
            )


class PersistenceTests(unittest.TestCase):
    def test_manual_review_updates_every_relation_for_exact_document_only(self):
        source_id = uuid4()
        document_id = uuid4()
        rows = [
            target_row(document_id, relation_type="APPLICATION"),
            target_row(document_id, relation_type="REFERENCE_1"),
        ]
        conn = RecordingConnection(
            locked_target_responses(document_id, rows),
            update_rows=[("APPLICATION",), ("REFERENCE_1",)],
        )

        updated = apply_manual_review(
            conn,
            policy_no="P1",
            source_id=str(source_id),
            document_id=str(document_id),
            decision="VERIFIED",
            reviewer="reviewer",
            reason="공식 공고의 정책명과 신청 내용이 일치함",
        )

        self.assertEqual(updated, 2)
        combined_sql = "\n".join(
            sql for sql, _ in conn.recording_cursor.executions
        )
        update_sql, update_params = next(
            (sql, params)
            for sql, params in conn.recording_cursor.executions
            if sql.lstrip().upper().startswith("UPDATE")
        )
        self.assertIn("UPDATE policy_web_source", update_sql)
        self.assertIn("validation_status = 'REVIEW_REQUIRED'", update_sql)
        self.assertIn("validated_document_id = %s", update_sql)
        self.assertIn("reviewed_at IS NULL", update_sql)
        self.assertIn("web_document", update_sql)
        self.assertIn("is_current = TRUE", update_sql)
        self.assertIn("validation_score = NULL", update_sql)
        self.assertNotIn(1.0, update_params)
        self.assertNotIn("UPDATE policy\n", combined_sql)
        self.assertNotIn("UPDATE policy_chunk", combined_sql)

    def test_partial_relation_update_is_rejected(self) -> None:
        document_id = uuid4()
        rows = [
            target_row(document_id, relation_type="APPLICATION"),
            target_row(document_id, relation_type="REFERENCE_1"),
        ]
        conn = RecordingConnection(
            locked_target_responses(document_id, rows),
            update_rows=[("APPLICATION",)],
        )

        with self.assertRaisesRegex(RuntimeError, "no partial decision"):
            apply_manual_review(
                conn,
                policy_no="P1",
                source_id=str(uuid4()),
                document_id=str(document_id),
                decision="LINK_ONLY",
                reviewer="reviewer",
                reason="일반 포털로서 링크 탐색에만 사용",
            )


if __name__ == "__main__":
    unittest.main()
