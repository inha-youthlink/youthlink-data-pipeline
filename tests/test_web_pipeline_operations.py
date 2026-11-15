from pathlib import Path
import contextlib
import io
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from etl.apply_migrations import MIGRATIONS
from etl.check_pipeline_health import health_problems
from etl.run_web_pipeline import (
    STEP_NAMES,
    build_commands,
    command_label,
    ensure_policy_pipeline_ready,
    load_latest_failed_job,
    parse_args,
    wait_for_policy_pipeline_ready,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "db" / "migrations" / "020_add_web_pipeline_operations.sql"
TRIAGE_MIGRATION = (
    REPO_ROOT / "db" / "migrations" / "021_add_web_pipeline_ai_triage.sql"
)
SCHEMA = REPO_ROOT / "db" / "schema.sql"


class WebPipelineOperationsTests(unittest.TestCase):
    def test_pipeline_order_covers_bounded_crawl_review_and_publish(self) -> None:
        self.assertEqual(
            STEP_NAMES,
            (
                "SOURCE_SYNC",
                "CRAWL",
                "REVALIDATE",
                "AI_SHADOW",
                "AI_APPLY",
                "AI_TRIAGE",
                "WEB_CHUNKS",
                "WEB_EMBEDDINGS",
                "RAG_PUBLISH",
                "VERIFY",
            ),
        )
        commands = build_commands(
            crawl_limit=50,
            per_host_limit=2,
            ai_review_limit=20,
            revalidate_limit=500,
        )
        self.assertIn("sync_policy_sources.py", command_label(commands["SOURCE_SYNC"]))
        self.assertIn("--per-host-limit", commands["CRAWL"])
        self.assertIn("--skip-cached", commands["AI_SHADOW"])
        self.assertIn("--apply-safe-observations", commands["AI_APPLY"])
        self.assertIn(
            "--apply-safe-link-only-observations",
            commands["AI_TRIAGE"],
        )
        self.assertIn("--retry-errors", commands["WEB_EMBEDDINGS"])
        self.assertIsNone(commands["VERIFY"])

    def test_parser_rejects_unbounded_or_conflicting_resume_values(self) -> None:
        args = parse_args(
            [
                "--crawl-limit",
                "25",
                "--per-host-limit",
                "1",
                "--ai-review-limit",
                "5",
            ]
        )
        self.assertEqual(args.crawl_limit, 25)
        self.assertEqual(args.per_host_limit, 1)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--crawl-limit", "0"])
            with self.assertRaises(SystemExit):
                parse_args(
                    ["--resume-job-id", "job", "--resume-latest-failed"]
                )
            with self.assertRaises(SystemExit):
                parse_args(["--policy-wait-minutes", "-1"])

    def test_web_pipeline_waits_for_successful_policy_pipeline(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            "FAILED",
            "POLICY_SYNC",
            False,
        )

        with self.assertRaisesRegex(RuntimeError, "web pipeline will retry"):
            ensure_policy_pipeline_ready(conn, 6)

        conn.execute.return_value.fetchone.return_value = (
            "SUCCEEDED",
            "COMPLETED",
            True,
        )
        ensure_policy_pipeline_ready(conn, 6)

        conn.execute.return_value.fetchone.return_value = (
            "SUCCEEDED",
            "COMPLETED",
            False,
        )
        with self.assertRaisesRegex(RuntimeError, "too old"):
            ensure_policy_pipeline_ready(conn, 6)
        with self.assertRaisesRegex(RuntimeError, "within 0 minutes"):
            wait_for_policy_pipeline_ready(
                conn,
                max_age_hours=6,
                wait_minutes=0,
                poll_seconds=0,
            )

    def test_latest_failed_web_job_must_be_recent(self) -> None:
        job_id = uuid4()
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            job_id,
            "FAILED",
            "WEB_EMBEDDINGS",
            True,
        )

        self.assertEqual(
            load_latest_failed_job(conn, 12),
            (str(job_id), "WEB_EMBEDDINGS"),
        )

    def test_health_flags_failed_web_pipeline(self) -> None:
        health = {
            "latest_policy_sync": {"status": "SUCCEEDED"},
            "latest_pipeline": {"status": "SUCCEEDED"},
            "latest_web_pipeline": {
                "status": "FAILED",
                "current_step": "CRAWL",
            },
            "unresolved_regions": 0,
            "policy_embedding_errors": 0,
            "web_embedding_errors": 0,
            "policy_embedding_pending": 0,
            "web_embedding_pending": 0,
        }

        self.assertEqual(
            health_problems(health),
            ["latest web pipeline failed at CRAWL"],
        )

    def test_schema_and_migration_include_web_pipeline_history(self) -> None:
        self.assertEqual(MIGRATIONS[-1], "021_add_web_pipeline_ai_triage.sql")
        for sql in (
            MIGRATION.read_text(encoding="utf-8"),
            SCHEMA.read_text(encoding="utf-8"),
        ):
            self.assertIn("web_pipeline_job", sql)
            self.assertIn("web_pipeline_step", sql)
            self.assertIn("AI_SHADOW", sql)
            self.assertIn("WEB_EMBEDDINGS", sql)
        triage_sql = TRIAGE_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("AI_TRIAGE", triage_sql)
        self.assertIn("BETWEEN 1 AND 10", triage_sql)


if __name__ == "__main__":
    unittest.main()
