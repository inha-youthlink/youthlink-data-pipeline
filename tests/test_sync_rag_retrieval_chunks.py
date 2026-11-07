from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from etl import sync_rag_retrieval_chunks as retrieval


class RecordingCursor:
    def __init__(self, *, rows=None, row=None, rowcount=0) -> None:
        self.rows = [] if rows is None else rows
        self.row = row
        self.rowcount = rowcount
        self.executions: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class RecordingConnection:
    def __init__(self, cursor: RecordingCursor | None = None) -> None:
        self.recording_cursor = cursor or RecordingCursor()

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor


class RetrievalSnapshotTests(unittest.TestCase):
    def test_snapshot_uses_only_active_api_and_verified_current_web_chunks(self):
        conn = RecordingConnection()

        retrieval.create_desired_snapshot(
            conn,
            policy_nos=("P1", "P2"),
        )

        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("CREATE TEMP TABLE desired_rag_retrieval_chunk", sql)
        self.assertIn("policy_row.is_active = TRUE", sql)
        self.assertIn("FROM v_verified_policy_web_chunk AS verified", sql)
        self.assertIn("pc.embedding_error IS NULL", sql)
        self.assertIn("web_chunk.embedding_error IS NULL", sql)
        self.assertIn("verified.canonical_url", sql)
        self.assertIn("verified.final_url", sql)
        self.assertIn("verified.fetched_at AS source_fetched_at", sql)
        self.assertIn("pc.embedding_encoding_format", sql)
        self.assertIn("verified.embedding_encoding_format", sql)
        self.assertEqual(params, [["P1", "P2"], ["P1", "P2"]])

        all_sql = "\n".join(
            statement for statement, _ in conn.recording_cursor.executions
        )
        self.assertNotIn("UPDATE policy_chunk", all_sql)
        self.assertNotIn("UPDATE policy_web_chunk", all_sql)
        self.assertNotIn("DELETE FROM policy_chunk", all_sql)
        self.assertNotIn("DELETE FROM policy_web_chunk", all_sql)

    def test_dry_run_snapshot_omits_large_payload_copies(self) -> None:
        conn = RecordingConnection()

        retrieval.create_desired_snapshot(conn, include_payload=False)

        sql, _ = conn.recording_cursor.executions[0]
        self.assertIn("NULL::TEXT AS content", sql)
        self.assertIn("NULL::VECTOR(1024) AS embedding", sql)
        self.assertNotIn("pc.content AS content", sql)
        self.assertNotIn("pc.embedding AS embedding", sql)

    def test_schema_error_names_required_migration(self) -> None:
        conn = RecordingConnection(RecordingCursor(rows=[]))

        with self.assertRaisesRegex(
            RuntimeError,
            "018_track_float_embedding_format.sql",
        ):
            retrieval.verify_schema(conn)

    def test_publish_lock_fails_fast_when_another_job_owns_it(self) -> None:
        conn = RecordingConnection(RecordingCursor(row=(False,)))

        with self.assertRaisesRegex(RuntimeError, "already running"):
            retrieval.acquire_publish_lock(conn)

        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("pg_try_advisory_xact_lock", sql)
        self.assertEqual(params, (retrieval.LOCK_NAME,))


class RetrievalPersistenceTests(unittest.TestCase):
    def test_change_detection_uses_hash_and_embedding_lineage_not_large_values(self) -> None:
        predicate = retrieval._changed_predicate("target", "desired")

        self.assertIn("target.content_hash", predicate)
        self.assertIn("target.embedding_model", predicate)
        self.assertIn("target.embedding_encoding_format", predicate)
        self.assertIn("target.embedded_at", predicate)
        self.assertNotIn("target.content,", predicate)
        self.assertNotIn("target.embedding,", predicate)

    def test_upsert_writes_only_the_disposable_projection(self) -> None:
        conn = RecordingConnection(RecordingCursor(rowcount=3))

        affected = retrieval._upsert_source(
            conn,
            source_type=retrieval.API_SOURCE_TYPE,
            conflict_column="policy_chunk_id",
        )

        self.assertEqual(affected, 3)
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("INSERT INTO rag_retrieval_chunk", sql)
        self.assertIn("ON CONFLICT (policy_chunk_id) DO UPDATE", sql)
        self.assertIn("IS DISTINCT FROM", sql)
        self.assertEqual(params, (retrieval.API_SOURCE_TYPE,))
        self.assertNotIn("UPDATE policy_chunk", sql)
        self.assertNotIn("UPDATE policy_web_chunk", sql)

    def test_scoped_stale_delete_cannot_touch_other_policies(self) -> None:
        conn = RecordingConnection(RecordingCursor(rowcount=2))

        deleted = retrieval._delete_stale(conn, policy_nos=("P1",))

        self.assertEqual(deleted, 2)
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("DELETE FROM rag_retrieval_chunk AS target", sql)
        self.assertIn("target.policy_no = ANY(%s)", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn(
            "desired.policy_chunk_id = target.policy_chunk_id",
            sql,
        )
        self.assertIn("desired.web_chunk_id = target.web_chunk_id", sql)
        self.assertEqual(sql.count("("), sql.count(")"))
        self.assertEqual(params, [["P1"]])

    def test_delta_count_uses_each_unique_origin_index_without_or_join(self) -> None:
        conn = RecordingConnection(RecordingCursor(row=(0, 0, 0)))

        stats = retrieval._load_delta_counts(conn, policy_nos=())

        self.assertEqual(stats, (0, 0, 0, 0))
        api_sql = conn.recording_cursor.executions[0][0]
        web_sql = conn.recording_cursor.executions[1][0]
        self.assertIn(
            "target.policy_chunk_id = desired.policy_chunk_id",
            api_sql,
        )
        self.assertNotIn("target.web_chunk_id = desired.web_chunk_id", api_sql)
        self.assertIn(
            "target.web_chunk_id = desired.web_chunk_id",
            web_sql,
        )
        self.assertNotIn(
            "target.policy_chunk_id = desired.policy_chunk_id",
            web_sql,
        )

    @patch.object(retrieval, "_load_delta_counts", return_value=(2, 1, 4, 1))
    @patch.object(
        retrieval,
        "_load_source_counts",
        return_value=(10, 9, 1, 0, 3, 2, 1, 0),
    )
    @patch.object(retrieval, "create_desired_snapshot")
    @patch.object(retrieval, "verify_schema")
    def test_dry_run_plans_without_acquiring_or_publishing(
        self,
        verify_schema: Mock,
        create_snapshot: Mock,
        load_sources: Mock,
        load_deltas: Mock,
    ) -> None:
        conn = object()

        with patch.object(retrieval, "acquire_publish_lock") as lock:
            with patch.object(retrieval, "_upsert_source") as upsert:
                stats = retrieval.plan_retrieval_sync(
                    conn,
                    policy_nos=(" P1 ", "P1"),
                )

        verify_schema.assert_called_once_with(conn)
        create_snapshot.assert_called_once_with(
            conn,
            policy_nos=("P1",),
            include_payload=False,
        )
        load_sources.assert_called_once_with(conn, policy_nos=("P1",))
        load_deltas.assert_called_once_with(conn, policy_nos=("P1",))
        lock.assert_not_called()
        upsert.assert_not_called()
        self.assertEqual(stats.desired_total, 11)
        self.assertEqual(stats.inserted, 2)
        self.assertEqual(stats.deleted, 1)

    @patch.object(retrieval, "_delete_stale", return_value=1)
    @patch.object(retrieval, "_upsert_source", side_effect=(2, 1))
    @patch.object(retrieval, "_load_delta_counts", return_value=(2, 1, 4, 1))
    @patch.object(
        retrieval,
        "_load_source_counts",
        return_value=(10, 9, 1, 0, 3, 2, 1, 0),
    )
    @patch.object(retrieval, "create_desired_snapshot")
    @patch.object(retrieval, "acquire_publish_lock")
    @patch.object(retrieval, "verify_schema")
    def test_write_locks_upserts_both_sources_and_deletes_stale_rows(
        self,
        verify_schema: Mock,
        acquire_lock: Mock,
        create_snapshot: Mock,
        load_sources: Mock,
        load_deltas: Mock,
        upsert: Mock,
        delete_stale: Mock,
    ) -> None:
        conn = object()

        stats = retrieval.sync_retrieval_chunks(
            conn,
            policy_nos=("P1",),
        )

        verify_schema.assert_called_once_with(conn)
        acquire_lock.assert_called_once_with(conn)
        create_snapshot.assert_called_once_with(conn, policy_nos=("P1",))
        self.assertEqual(
            upsert.call_args_list,
            [
                call(
                    conn,
                    source_type=retrieval.API_SOURCE_TYPE,
                    conflict_column="policy_chunk_id",
                ),
                call(
                    conn,
                    source_type=retrieval.WEB_SOURCE_TYPE,
                    conflict_column="web_chunk_id",
                ),
            ],
        )
        delete_stale.assert_called_once_with(conn, policy_nos=("P1",))
        self.assertEqual(stats.updated, 1)
        self.assertEqual(stats.unchanged, 4)

    @patch.object(retrieval, "_delete_stale", return_value=0)
    @patch.object(retrieval, "_upsert_source", side_effect=(1, 0))
    @patch.object(retrieval, "_load_delta_counts", return_value=(2, 0, 0, 0))
    @patch.object(
        retrieval,
        "_load_source_counts",
        return_value=(2, 2, 0, 0, 0, 0, 0, 0),
    )
    @patch.object(retrieval, "create_desired_snapshot")
    @patch.object(retrieval, "acquire_publish_lock")
    @patch.object(retrieval, "verify_schema")
    def test_write_aborts_when_applied_counts_do_not_match_snapshot(
        self,
        *_mocks,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            retrieval.sync_retrieval_chunks(object())


class ArgumentTests(unittest.TestCase):
    def test_defaults_to_dry_run_and_deduplicates_policy_scope(self) -> None:
        args = retrieval.parse_args(
            ["--policy-no", "P1", "--policy-no", "P1"]
        )

        self.assertFalse(args.write)
        self.assertEqual(
            retrieval.normalize_policy_nos(args.policy_no),
            ("P1",),
        )


if __name__ == "__main__":
    unittest.main()
