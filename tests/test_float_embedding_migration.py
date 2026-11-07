from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    REPO_ROOT
    / "db"
    / "migrations"
    / "018_track_float_embedding_format.sql"
)
SCHEMA = REPO_ROOT / "db" / "schema.sql"


class FloatEmbeddingMigrationTests(unittest.TestCase):
    def test_migration_is_atomic_and_uses_fixed_search_path(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertEqual(sql.count("BEGIN;"), 1)
        self.assertEqual(sql.count("COMMIT;"), 1)
        self.assertIn("SET LOCAL search_path = pg_catalog, public;", sql)

    def test_migration_tracks_format_on_all_embedding_tables(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")

        for table in (
            "public.policy_chunk",
            "public.policy_web_chunk",
            "public.rag_retrieval_chunk",
        ):
            self.assertIn(f"ALTER TABLE {table}", sql)
        self.assertGreaterEqual(sql.count("embedding_encoding_format"), 12)
        self.assertIn("embedding_encoding_format = 'float'", sql)

    def test_migration_discards_only_unlabelled_derived_vectors(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")

        self.assertEqual(sql.count("WHERE embedding IS NOT NULL"), 2)
        self.assertIn("DELETE FROM public.rag_retrieval_chunk", sql)
        self.assertIn("WHERE embedding_encoding_format IS NULL", sql)
        self.assertNotIn("DELETE FROM public.policy_chunk", sql)
        self.assertNotIn("DELETE FROM public.policy_web_chunk", sql)

    def test_fresh_schema_requires_float_format(self) -> None:
        sql = SCHEMA.read_text(encoding="utf-8")

        self.assertGreaterEqual(sql.count("embedding_encoding_format"), 8)
        self.assertGreaterEqual(
            sql.count("embedding_encoding_format = 'float'"),
            3,
        )


if __name__ == "__main__":
    unittest.main()
