import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "017_add_policy_web_ai_review.sql"
)
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"


class PolicyWebAiReviewSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.migration = MIGRATION_PATH.read_text(encoding="utf-8")
        cls.schema = SCHEMA_PATH.read_text(encoding="utf-8")
        cls.normalized_migration = re.sub(r"\s+", " ", cls.migration).strip()

    def test_migration_is_atomic_and_rerunnable(self):
        self.assertEqual(self.migration.count("BEGIN;"), 1)
        self.assertEqual(self.migration.count("COMMIT;"), 1)
        self.assertTrue(self.normalized_migration.endswith("COMMIT;"))
        self.assertIn("SET LOCAL search_path = pg_catalog, public;", self.migration)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS public.policy_web_ai_review",
            self.migration,
        )
        self.assertEqual(self.migration.count("CREATE INDEX IF NOT EXISTS"), 2)
        self.assertEqual(
            self.migration.count("CREATE UNIQUE INDEX IF NOT EXISTS"), 1
        )

    def test_migration_and_schema_share_the_observation_contract(self):
        required_fragments = (
            "ai_review_id",
            "policy_no",
            "source_id",
            "document_id",
            "content_hash",
            "policy_evidence_hash",
            "previous_status",
            "model",
            "prompt_version",
            "reviewer_version",
            "execution_status",
            "page_type",
            "identity_result",
            "raw_response",
            "identity_evidence",
            "supporting_evidence",
            "conflicting_evidence",
            "lifecycle_flags",
            "rejected_evidence",
            "proposed_status",
            "proposed_score",
            "verification_result",
            "failure_reason",
            "input_token_count",
            "output_token_count",
            "created_at",
            "applied_at",
            "metadata",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.migration)
                self.assertIn(fragment, self.schema)

    def test_observation_is_bound_to_exact_policy_source_document_and_hash(self):
        required_constraints = (
            "fk_policy_web_ai_review_policy",
            "fk_policy_web_ai_review_source_document",
            "uq_policy_web_ai_review_success_cache",
            "chk_policy_web_ai_review_content_hash",
            "chk_policy_web_ai_review_policy_evidence_hash",
            "chk_policy_web_ai_review_previous_status",
        )
        for constraint in required_constraints:
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, self.migration)
                self.assertIn(constraint, self.schema)
        self.assertIn(
            "REFERENCES public.web_document (source_id, document_id)",
            self.migration,
        )
        self.assertRegex(
            self.normalized_migration,
            r"CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_web_ai_review_success_cache ON public\.policy_web_ai_review \( policy_no, source_id, document_id, content_hash, policy_evidence_hash, model, prompt_version, reviewer_version \) WHERE execution_status = 'SUCCEEDED';",
        )

    def test_only_successful_observations_are_cached(self):
        cache_start = self.migration.index(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_web_ai_review_success_cache"
        )
        cache_end = self.migration.index(";", cache_start)
        cache_sql = self.migration[cache_start:cache_end]
        self.assertIn("policy_evidence_hash", cache_sql)
        self.assertIn("WHERE execution_status = 'SUCCEEDED'", cache_sql)
        self.assertNotIn("FAILED", cache_sql)
        self.assertNotIn("INVALID_OUTPUT", cache_sql)

    def test_untrusted_model_fields_are_structurally_constrained(self):
        for sql in (self.migration, self.schema):
            self.assertIn("chk_policy_web_ai_review_json_arrays", sql)
            self.assertIn("jsonb_typeof(identity_evidence) = 'array'", sql)
            self.assertIn("chk_policy_web_ai_review_raw_response", sql)
            self.assertIn("chk_policy_web_ai_review_page_type", sql)
            self.assertIn("chk_policy_web_ai_review_identity_result", sql)
            self.assertIn("chk_policy_web_ai_review_proposed_status", sql)
            self.assertIn("chk_policy_web_ai_review_token_counts", sql)
            self.assertIn("chk_policy_web_ai_review_verification_result", sql)
            self.assertIn("chk_policy_web_ai_review_execution_state", sql)
            self.assertIn("chk_policy_web_ai_review_failure_reason", sql)
            self.assertIn("failure_reason        TEXT NOT NULL", sql)

    def test_success_and_non_success_rows_have_consistent_shapes(self):
        for sql in (self.migration, self.schema):
            self.assertIn("proposed_status IN ('VERIFIED', 'REJECTED')", sql)
            self.assertIn("verification_result = 'ACCEPTED'", sql)
            self.assertIn("proposed_status = 'REVIEW_REQUIRED'", sql)
            self.assertIn("verification_result = 'INSUFFICIENT'", sql)
            self.assertIn(
                "execution_status IN ('FAILED', 'INVALID_OUTPUT')",
                sql,
            )
            self.assertIn("jsonb_array_length(rejected_evidence) = 0", sql)

    def test_only_code_accepted_terminal_proposals_can_be_marked_applied(self):
        for sql in (self.migration, self.schema):
            self.assertIn("chk_policy_web_ai_review_applied_state", sql)
            self.assertIn("execution_status = 'SUCCEEDED'", sql)
            self.assertIn("proposed_status IN ('VERIFIED', 'REJECTED')", sql)
            self.assertIn("verification_result = 'ACCEPTED'", sql)

    def test_migration_does_not_change_canonical_review_state(self):
        forbidden_patterns = (
            r"(?i)UPDATE\s+(?:public\.)?policy_web_source",
            r"(?i)DELETE\s+FROM\s+(?:public\.)?policy_web_source",
            r"(?i)ALTER\s+TABLE\s+(?:public\.)?policy_web_source",
            r"(?i)INSERT\s+INTO\s+(?:public\.)?policy_web_source",
        )
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotRegex(self.migration, pattern)


if __name__ == "__main__":
    unittest.main()
