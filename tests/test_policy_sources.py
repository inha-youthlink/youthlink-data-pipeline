from __future__ import annotations

import unittest
from uuid import uuid4

from etl.sync_policy_sources import (
    DiscoveryResult,
    PolicySourceLink,
    UrlNormalizationError,
    discover_policy_sources,
    load_active_policy_urls,
    normalize_url,
    sync_policy_sources,
)


class UrlNormalizationTests(unittest.TestCase):
    def test_adds_https_normalizes_host_and_removes_fragment(self) -> None:
        result = normalize_url(
            "  WWW.Example.COM:443/policy detail?a=1#application  "
        )

        self.assertEqual(result.scheme, "https")
        self.assertEqual(result.host, "www.example.com")
        self.assertEqual(
            result.canonical_url,
            "https://www.example.com/policy%20detail?a=1",
        )

    def test_removes_default_http_port_but_preserves_query(self) -> None:
        result = normalize_url("http://Example.com:80/view?id=10&kind=A")

        self.assertEqual(
            result.canonical_url,
            "http://example.com/view?id=10&kind=A",
        )

    def test_rejects_non_http_scheme(self) -> None:
        with self.assertRaises(UrlNormalizationError) as raised:
            normalize_url("ftp://example.com/file")

        self.assertEqual(raised.exception.code, "UNSUPPORTED_SCHEME")

    def test_rejects_credentials_and_non_public_destinations(self) -> None:
        values = (
            "https://user:password@example.com/policy",
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data",
            "http://localhost/policy",
            "http://internal/policy",
        )

        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(UrlNormalizationError):
                    normalize_url(value)


class DiscoveryTests(unittest.TestCase):
    def test_discovers_only_valid_links_and_deduplicates_same_relation(self) -> None:
        result = discover_policy_sources(
            [
                {
                    "policy_no": "P1",
                    "application_url": "example.com/app#top",
                    "reference_url_1": "https://example.com/app",
                    "reference_url_2": "http://127.0.0.1/private",
                },
                {
                    "policy_no": "P2",
                    "application_url": None,
                    "reference_url_1": "https://example.com/app",
                    "reference_url_2": "https://other.example.org/detail",
                },
            ]
        )

        self.assertEqual(result.active_policy_count, 2)
        self.assertEqual(len(result.links), 4)
        self.assertEqual(result.unique_source_count, 2)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].code, "BLOCKED_ADDRESS")

    def test_invalid_values_are_reported_without_echoing_raw_url(self) -> None:
        secret_url = "https://user:secret@example.com/policy"
        result = discover_policy_sources(
            [{"policy_no": "P1", "application_url": secret_url}]
        )

        self.assertEqual(len(result.links), 0)
        self.assertEqual(len(result.issues), 1)
        self.assertNotIn(secret_url, result.issues[0].reason)


class RecordingCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executions: list[tuple[str, object]] = []
        self.rowcount = 0
        self._next_source_id = uuid4()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        if "UPDATE policy_web_source AS link" in sql:
            self.rowcount = 2

    def fetchone(self):
        return (self._next_source_id,)

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self, rows=None):
        self.recording_cursor = RecordingCursor(rows)

    def cursor(self, **kwargs):
        return self.recording_cursor


class PersistenceTests(unittest.TestCase):
    def test_load_urls_selects_only_active_policies(self) -> None:
        conn = RecordingConnection([])

        rows = load_active_policy_urls(conn)

        self.assertEqual(rows, [])
        sql = conn.recording_cursor.executions[0][0]
        self.assertIn("WHERE is_active = TRUE", sql)

    def test_sync_keeps_new_links_pending_and_soft_deactivates_missing_links(self):
        link = PolicySourceLink(
            policy_no="P1",
            relation_type="REFERENCE_1",
            original_url="https://example.com/policy",
            canonical_url="https://example.com/policy",
            scheme="https",
            host="example.com",
        )
        result = DiscoveryResult(
            active_policy_count=1,
            links=(link,),
            issues=(),
        )
        conn = RecordingConnection()

        stats = sync_policy_sources(conn, result)

        combined_sql = "\n".join(
            sql for sql, _ in conn.recording_cursor.executions
        )
        link_upsert = next(
            sql
            for sql, _ in conn.recording_cursor.executions
            if "INSERT INTO policy_web_source" in sql
        )
        self.assertEqual(stats["sources"], 1)
        self.assertEqual(stats["links"], 1)
        self.assertEqual(stats["deactivated"], 2)
        insert_columns = link_upsert.split("VALUES", 1)[0]
        self.assertNotIn("validation_status", insert_columns)
        self.assertIn("UPDATE policy_web_source AS link", combined_sql)
        self.assertIn("SET is_active = FALSE", combined_sql)

    def test_reactivated_link_resets_stale_validation(self):
        link = PolicySourceLink(
            policy_no="P1",
            relation_type="REFERENCE_1",
            original_url="https://example.com/policy",
            canonical_url="https://example.com/policy",
            scheme="https",
            host="example.com",
        )
        conn = RecordingConnection()

        sync_policy_sources(
            conn,
            DiscoveryResult(active_policy_count=1, links=(link,), issues=()),
        )

        link_upsert = next(
            sql
            for sql, _ in conn.recording_cursor.executions
            if "INSERT INTO policy_web_source" in sql
        )
        self.assertIn("policy_web_source.is_active = FALSE", link_upsert)
        self.assertIn("validated_document_id", link_upsert)
        self.assertIn("THEN 'PENDING'", link_upsert)


if __name__ == "__main__":
    unittest.main()
