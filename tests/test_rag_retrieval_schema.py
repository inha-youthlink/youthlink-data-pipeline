import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "016_add_rag_retrieval_chunks.sql"
)
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"


class RagRetrievalSchemaTests(unittest.TestCase):
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
            "CREATE TABLE IF NOT EXISTS public.rag_retrieval_chunk",
            self.migration,
        )
        self.assertEqual(self.migration.count("CREATE INDEX IF NOT EXISTS"), 2)

    def test_migration_and_schema_contain_the_same_projection_contract(self):
        required_fragments = (
            "retrieval_chunk_id",
            "policy_no",
            "source_type",
            "policy_chunk_id",
            "web_chunk_id",
            "document_id",
            "source_id",
            "canonical_url",
            "final_url",
            "source_fetched_at",
            "chunk_index",
            "chunk_type",
            "heading",
            "content_hash",
            "chunker_version",
            "embedding             VECTOR(1024) NOT NULL",
            "embedding_model",
            "token_count",
            "embedded_at",
            "source_updated_at",
            "source_priority",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.migration)
                self.assertIn(fragment, self.schema)

    def test_each_published_row_has_exactly_one_typed_origin(self):
        for sql in (self.migration, self.schema):
            with self.subTest(sql="migration" if sql is self.migration else "schema"):
                self.assertIn("CONSTRAINT chk_rag_retrieval_chunk_origin", sql)
                self.assertIn("source_type = 'API'", sql)
                self.assertIn("source_type = 'CRAWLED_WEB'", sql)
                self.assertIn("policy_chunk_id IS NOT NULL", sql)
                self.assertIn("web_chunk_id IS NOT NULL", sql)
                self.assertIn("document_id IS NOT NULL", sql)
                self.assertIn("source_id IS NOT NULL", sql)
                self.assertIn("canonical_url IS NOT NULL", sql)
                self.assertIn("source_fetched_at IS NOT NULL", sql)

    def test_source_specific_chunk_types_are_enforced(self):
        for sql in (self.migration, self.schema):
            self.assertIn("CONSTRAINT chk_rag_retrieval_chunk_type", sql)
            self.assertIn(
                "'SUMMARY', 'ELIGIBILITY', 'SUPPORT', 'APPLICATION'",
                sql,
            )
            self.assertIn("chunk_type = 'WEB_TEXT'", sql)

    def test_projection_preserves_source_foreign_keys_and_uniqueness(self):
        required_constraints = (
            "fk_rag_retrieval_chunk_policy",
            "fk_rag_retrieval_chunk_policy_chunk",
            "fk_rag_retrieval_chunk_web_chunk",
            "fk_rag_retrieval_chunk_document",
            "fk_rag_retrieval_chunk_source",
            "uq_rag_retrieval_chunk_policy_chunk",
            "uq_rag_retrieval_chunk_web_chunk",
        )
        for constraint in required_constraints:
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, self.migration)
                self.assertIn(constraint, self.schema)
        self.assertEqual(self.migration.count("ON DELETE CASCADE"), 5)

    def test_projection_has_one_physical_cosine_hnsw_index(self):
        migration_hnsw = re.findall(
            r"CREATE INDEX IF NOT EXISTS\s+\w+\s+"
            r"ON public\.rag_retrieval_chunk\s+"
            r"USING hnsw \(embedding vector_cosine_ops\)",
            self.migration,
            flags=re.IGNORECASE,
        )
        schema_hnsw = re.findall(
            r"CREATE INDEX IF NOT EXISTS\s+\w+\s+"
            r"ON rag_retrieval_chunk\s+"
            r"USING hnsw \(embedding vector_cosine_ops\)",
            self.schema,
            flags=re.IGNORECASE,
        )
        self.assertEqual(len(migration_hnsw), 1)
        self.assertEqual(len(schema_hnsw), 1)

    def test_migration_does_not_mutate_source_corpora(self):
        forbidden_patterns = (
            r"(?i)INSERT\s+INTO\s+(?:public\.)?policy_chunk",
            r"(?i)UPDATE\s+(?:public\.)?policy_chunk",
            r"(?i)DELETE\s+FROM\s+(?:public\.)?policy_chunk",
            r"(?i)ALTER\s+TABLE\s+(?:public\.)?policy_chunk",
            r"(?i)INSERT\s+INTO\s+(?:public\.)?policy_web_chunk",
            r"(?i)UPDATE\s+(?:public\.)?policy_web_chunk",
            r"(?i)DELETE\s+FROM\s+(?:public\.)?policy_web_chunk",
            r"(?i)ALTER\s+TABLE\s+(?:public\.)?policy_web_chunk",
        )
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotRegex(self.migration, pattern)


if __name__ == "__main__":
    unittest.main()
