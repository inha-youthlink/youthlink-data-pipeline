import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "015_converge_chat_schema.sql"


class ChatSchemaMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION_PATH.read_text(encoding="utf-8")
        cls.normalized_sql = re.sub(r"\s+", " ", cls.sql).strip()

    def test_migration_is_atomic_and_keeps_shared_history_files(self):
        self.assertTrue(self.normalized_sql.startswith("BEGIN;"))
        self.assertTrue(self.normalized_sql.endswith("COMMIT;"))
        self.assertTrue(
            (REPO_ROOT / "db" / "migrations" / "008_add_chat_state.sql").exists()
        )
        self.assertTrue(
            (
                REPO_ROOT
                / "db"
                / "migrations"
                / "009_add_chat_idempotency_and_scrap.sql"
            ).exists()
        )

    def test_unrecoverable_legacy_values_fail_before_not_null_enforcement(self):
        user_guard = self.sql.index(
            "cannot add chat_room.user_id safely: legacy chat_room rows exist"
        )
        order_guard = self.sql.index(
            "cannot add message_policy_ref.display_order safely: legacy reference rows exist"
        )
        user_not_null = self.sql.index("ALTER COLUMN user_id SET NOT NULL")
        order_not_null = self.sql.index("ALTER COLUMN display_order SET NOT NULL")

        self.assertLess(user_guard, user_not_null)
        self.assertLess(order_guard, order_not_null)
        self.assertIn("verified user-to-room mapping", self.sql)
        self.assertIn("verified user-visible order", self.sql)

    def test_risky_columns_are_added_nullable_then_enforced(self):
        self.assertRegex(
            self.normalized_sql,
            r"ALTER TABLE public\.chat_room ADD COLUMN IF NOT EXISTS user_id VARCHAR\(100\);",
        )
        self.assertRegex(
            self.normalized_sql,
            r"ALTER TABLE public\.message_policy_ref ADD COLUMN IF NOT EXISTS display_order SMALLINT;",
        )
        self.assertNotRegex(
            self.normalized_sql,
            r"ADD COLUMN IF NOT EXISTS user_id VARCHAR\(100\) NOT NULL",
        )
        self.assertNotRegex(
            self.normalized_sql,
            r"ADD COLUMN IF NOT EXISTS display_order SMALLINT NOT NULL",
        )

    def test_final_chat_objects_and_constraints_are_converged(self):
        required_fragments = (
            "CREATE TABLE IF NOT EXISTS public.chat_current_policy",
            "CREATE TABLE IF NOT EXISTS public.policy_scrap",
            "ADD COLUMN IF NOT EXISTS metadata JSONB",
            "ADD COLUMN IF NOT EXISTS client_message_id UUID",
            "ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT",
            "fk_chat_message_reply_to",
            "uq_chat_message_client_message_id",
            "uq_message_policy_ref_order",
            "chk_message_policy_ref_order",
            "fk_chat_current_policy_room",
            "fk_chat_current_policy_policy",
            "uq_chat_current_policy_order",
            "chk_chat_current_policy_order",
            "fk_policy_scrap_policy",
            "idx_chat_room_user_updated",
            "idx_chat_message_reply_to",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.sql)

    def test_migration_does_not_remove_or_rename_shared_objects(self):
        self.assertNotRegex(self.normalized_sql, r"(?i)DROP TABLE")
        self.assertNotRegex(self.normalized_sql, r"(?i)DROP COLUMN")
        self.assertNotRegex(self.normalized_sql, r"(?i)RENAME (?:TABLE|COLUMN|CONSTRAINT)")

    def test_named_constraints_are_checked_by_definition(self):
        self.assertIn("SET LOCAL search_path = pg_catalog, public", self.sql)
        self.assertLess(
            self.sql.index("BEGIN;"),
            self.sql.index("SET LOCAL search_path = pg_catalog, public"),
        )
        self.assertIn("pg_get_constraintdef(constraint_row.oid, TRUE)", self.sql)
        self.assertIn("has an unexpected definition", self.sql)

        expected_names = (
            "fk_chat_message_reply_to",
            "uq_chat_message_client_message_id",
            "uq_message_policy_ref_order",
            "chk_message_policy_ref_order",
            "chat_current_policy_pkey",
            "fk_chat_current_policy_room",
            "fk_chat_current_policy_policy",
            "uq_chat_current_policy_order",
            "chk_chat_current_policy_order",
            "policy_scrap_pkey",
            "fk_policy_scrap_policy",
        )
        definition_check_start = self.sql.index(
            "-- 동명 제약이 있다는 사실만으로는 최종 구조를 보장할 수 없다."
        )
        definition_check_end = self.sql.index(
            "-- 올바른 정의지만 NOT VALID인 부분 적용 FK/CHECK"
        )
        definition_check = self.sql[definition_check_start:definition_check_end]
        for constraint_name in expected_names:
            with self.subTest(constraint_name=constraint_name):
                self.assertIn(constraint_name, definition_check)

    def test_alternate_named_constraints_require_full_semantics(self):
        reuse_start = self.sql.index(
            "-- 기대 이름이 없고 다른 이름의 제약이 있는 부분 적용 상태에서도"
        )
        reuse_end = self.sql.index(
            "-- CREATE INDEX IF NOT EXISTS는 동명 인덱스의 컬럼·정렬·predicate가 달라도",
            reuse_start,
        )
        reuse_check = self.sql[reuse_start:reuse_end]

        self.assertEqual(reuse_check.count("AND convalidated"), 9)
        self.assertEqual(reuse_check.count("AND NOT condeferrable"), 9)
        self.assertEqual(reuse_check.count("AND NOT condeferred"), 9)
        self.assertEqual(reuse_check.count("AND confupdtype = 'a'"), 4)
        self.assertEqual(reuse_check.count("AND confmatchtype = 's'"), 4)
        self.assertEqual(reuse_check.count("AND pg_get_constraintdef(oid, TRUE)"), 9)

    def test_named_indexes_are_checked_by_catalog_shape(self):
        index_check_start = self.sql.index(
            "-- CREATE INDEX IF NOT EXISTS는 동명 인덱스의 컬럼·정렬·predicate가 달라도"
        )
        index_check_end = self.sql.index(
            "CREATE INDEX IF NOT EXISTS idx_chat_room_user_updated",
            index_check_start,
        )
        index_check = self.sql[index_check_start:index_check_end]

        self.assertIn("JOIN pg_index AS index_catalog", index_check)
        self.assertIn("index_catalog.indkey::TEXT", index_check)
        self.assertIn("index_catalog.indoption::TEXT = '0 3'", index_check)
        self.assertIn("index_catalog.indoption::TEXT = '0'", index_check)
        self.assertIn("pg_get_expr(index_catalog.indpred", index_check)
        self.assertEqual(index_check.count("pg_get_indexdef(index_relation.oid)"), 2)
        self.assertIn("idx_chat_room_user_updated has an unexpected definition", index_check)
        self.assertIn("idx_chat_message_reply_to has an unexpected definition", index_check)


if __name__ == "__main__":
    unittest.main()
