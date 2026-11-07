from pathlib import Path
import contextlib
import io
import unittest

from etl.apply_migrations import MIGRATIONS
from etl.check_pipeline_health import health_problems
from etl.run_policy_pipeline import (
    STEP_NAMES,
    build_commands,
    command_label,
    parse_args,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    REPO_ROOT / "db" / "migrations" / "019_add_pipeline_operations.sql"
)
SCHEMA = REPO_ROOT / "db" / "schema.sql"


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
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--workers", "0"])

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

    def test_migration_order_resolves_duplicate_legacy_numbers_explicitly(self) -> None:
        self.assertIn("008_add_policy_sync_tracking.sql", MIGRATIONS)
        self.assertIn("009_add_policy_web_sources.sql", MIGRATIONS)
        self.assertNotIn("008_add_chat_state.sql", MIGRATIONS)
        self.assertNotIn("009_add_chat_idempotency_and_scrap.sql", MIGRATIONS)
        self.assertEqual(MIGRATIONS[-1], "019_add_pipeline_operations.sql")

    def test_operations_tables_exist_in_migration_and_fresh_schema(self) -> None:
        migration = MIGRATION.read_text(encoding="utf-8")
        schema = SCHEMA.read_text(encoding="utf-8")
        for sql in (migration, schema):
            self.assertIn("schema_migration", sql)
            self.assertIn("policy_pipeline_job", sql)
            self.assertIn("policy_pipeline_step", sql)
            self.assertIn("pg_catalog, public", sql if sql is migration else migration)


if __name__ == "__main__":
    unittest.main()
