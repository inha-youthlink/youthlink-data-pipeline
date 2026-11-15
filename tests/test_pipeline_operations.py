from pathlib import Path
import contextlib
import io
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from etl.apply_migrations import MIGRATIONS
from etl.check_pipeline_health import health_problems
from etl.run_policy_pipeline import (
    STEP_NAMES,
    build_commands,
    command_label,
    fail_orphaned_jobs,
    load_latest_failed_job,
    parse_args,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    REPO_ROOT / "db" / "migrations" / "019_add_pipeline_operations.sql"
)
SCHEMA = REPO_ROOT / "db" / "schema.sql"
SCHEDULE_RUNNER = REPO_ROOT / "ops" / "run_policy_pipeline.ps1"
SCHEDULE_REGISTER = REPO_ROOT / "ops" / "register_policy_pipeline_task.ps1"


class PipelineOperationsTests(unittest.TestCase):
    def test_pipeline_order_covers_sync_embed_publish_and_verify(self) -> None:
        self.assertEqual(
            STEP_NAMES,
            (
                "POLICY_SYNC",
                "POLICY_CHUNKS",
                "POLICY_EMBEDDINGS",
                "RAG_PUBLISH",
                "VERIFY",
            ),
        )
        commands = build_commands(3)
        self.assertIn("load_policies.py", command_label(commands["POLICY_SYNC"]))
        self.assertIn("--write", commands["POLICY_CHUNKS"])
        self.assertIn("--retry-errors", commands["POLICY_EMBEDDINGS"])
        self.assertIn("3", commands["POLICY_EMBEDDINGS"])
        self.assertIsNone(commands["VERIFY"])

    def test_pipeline_parser_supports_safe_resume_controls(self) -> None:
        args = parse_args(["--start-at", "POLICY_CHUNKS", "--workers", "2"])
        self.assertEqual(args.start_at, "POLICY_CHUNKS")
        self.assertEqual(args.workers, 2)
        latest = parse_args(
            ["--resume-latest-failed", "--resume-max-age-hours", "4"]
        )
        self.assertTrue(latest.resume_latest_failed)
        self.assertEqual(latest.resume_max_age_hours, 4)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--workers", "0"])
            with self.assertRaises(SystemExit):
                parse_args(
                    ["--resume-job-id", "job", "--resume-latest-failed"]
                )

    def test_latest_failed_resume_requires_latest_recent_failure(self) -> None:
        job_id = uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            job_id,
            "FAILED",
            "POLICY_EMBEDDINGS",
            True,
        )

        selected = load_latest_failed_job(conn, 6)

        self.assertEqual(selected, (str(job_id), "POLICY_EMBEDDINGS"))
        params = conn.execute.call_args.args[1]
        self.assertEqual(params, (6,))

        conn.execute.return_value.fetchone.return_value = (
            job_id,
            "SUCCEEDED",
            "COMPLETED",
            True,
        )
        self.assertIsNone(load_latest_failed_job(conn, 6))

    def test_orphaned_running_jobs_are_closed_before_new_run(self) -> None:
        first = MagicMock()
        second = MagicMock()
        job_id = uuid4()
        second.fetchall.return_value = [(job_id,)]
        conn = MagicMock()
        conn.execute.side_effect = [first, second]

        closed = fail_orphaned_jobs(conn)

        self.assertEqual(closed, [str(job_id)])
        self.assertEqual(conn.execute.call_count, 2)
        conn.commit.assert_called_once_with()

    def test_health_ignores_historical_ai_failures_but_flags_etl_state(self) -> None:
        health = {
            "latest_policy_sync": {"status": "SUCCEEDED"},
            "latest_pipeline": None,
            "unresolved_regions": 0,
            "policy_embedding_errors": 0,
            "web_embedding_errors": 0,
            "policy_embedding_pending": 0,
            "web_embedding_pending": 0,
            "ai_review_failures": 3,
        }
        self.assertEqual(health_problems(health), [])
        health["unresolved_regions"] = 1
        self.assertEqual(health_problems(health), ["unresolved_regions=1"])

    def test_health_flags_stale_running_pipeline(self) -> None:
        health = {
            "latest_policy_sync": {"status": "SUCCEEDED"},
            "latest_pipeline": {
                "status": "RUNNING",
                "current_step": "POLICY_SYNC",
                "is_stale": True,
            },
            "unresolved_regions": 0,
            "policy_embedding_errors": 0,
            "web_embedding_errors": 0,
            "policy_embedding_pending": 0,
            "web_embedding_pending": 0,
        }

        self.assertEqual(
            health_problems(health),
            ["latest policy pipeline is stale at POLICY_SYNC"],
        )

    def test_migration_order_resolves_duplicate_legacy_numbers_explicitly(self) -> None:
        self.assertIn("008_add_policy_sync_tracking.sql", MIGRATIONS)
        self.assertIn("009_add_policy_web_sources.sql", MIGRATIONS)
        self.assertNotIn("008_add_chat_state.sql", MIGRATIONS)
        self.assertNotIn("009_add_chat_idempotency_and_scrap.sql", MIGRATIONS)
        self.assertEqual(MIGRATIONS[-2], "019_add_pipeline_operations.sql")

    def test_operations_tables_exist_in_migration_and_fresh_schema(self) -> None:
        migration = MIGRATION.read_text(encoding="utf-8")
        schema = SCHEMA.read_text(encoding="utf-8")
        for sql in (migration, schema):
            self.assertIn("schema_migration", sql)
            self.assertIn("policy_pipeline_job", sql)
            self.assertIn("policy_pipeline_step", sql)
            self.assertIn("pg_catalog, public", sql if sql is migration else migration)

    def test_windows_scheduler_uses_safe_integrated_entrypoint(self) -> None:
        runner = SCHEDULE_RUNNER.read_text(encoding="utf-8")
        register = SCHEDULE_REGISTER.read_text(encoding="utf-8")

        self.assertIn("etl/run_policy_pipeline.py", runner)
        self.assertIn("--resume-latest-failed", runner)
        self.assertIn("etl/check_pipeline_health.py", runner)
        self.assertIn("etl\\logs\\policy-pipeline", runner)
        self.assertIn("import psycopg, dotenv, requests, openai", runner)
        self.assertIn("import psycopg, dotenv, requests, openai", register)
        self.assertNotIn("LITELLM_API_KEY=", runner)
        self.assertNotIn("YOUTH_API_KEY=", runner)
        self.assertIn("RestartCount 2", register)
        self.assertIn("MultipleInstances IgnoreNew", register)
        self.assertIn("RunOnlyIfNetworkAvailable", register)


if __name__ == "__main__":
    unittest.main()
