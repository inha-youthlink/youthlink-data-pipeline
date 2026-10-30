from __future__ import annotations

import importlib
import io
import traceback
import unittest
from datetime import date
from unittest.mock import MagicMock, patch
from uuid import uuid4


load_policies = importlib.import_module("etl.load_policies")


class TransformPolicyTests(unittest.TestCase):
    def test_transform_policy_normalizes_age_range(self) -> None:
        record = load_policies.transform_policy(
            {
                "plcyNo": "P001",
                "sprtTrgtMinAge": "25",
                "sprtTrgtMaxAge": "20",
            }
        )

        self.assertEqual(record.policy["min_age"], 25)
        self.assertIsNone(record.policy["max_age"])

    def test_transform_policy_maps_core_fields_without_external_io(self) -> None:
        record = load_policies.transform_policy(
            {
                "plcyNo": " P002 ",
                "plcyNm": " 청년 정책 ",
                "zipCd": "11110, 11140",
                "schoolCd": "S1,S2",
                "aplyYmd": "20260101 ~ 20260131",
            }
        )

        self.assertEqual(record.policy["policy_no"], "P002")
        self.assertEqual(record.policy["policy_name"], "청년 정책")
        self.assertEqual(record.regions, ["11110", "11140"])
        self.assertEqual(
            record.eligibility_codes,
            [("SCHOOL", "S1"), ("SCHOOL", "S2")],
        )
        self.assertEqual(record.policy["application_start_date"], date(2026, 1, 1))
        self.assertEqual(record.policy["application_end_date"], date(2026, 1, 31))
        self.assertEqual(
            record.application_periods,
            ((date(2026, 1, 1), date(2026, 1, 31)),),
        )
        self.assertEqual(record.policy["application_period_parse_status"], "SINGLE")

    def test_transform_policy_preserves_discontinuous_application_periods(self):
        record = load_policies.transform_policy(
            {
                "plcyNo": "P003",
                "aplyYmd": "20261001 ~ 20261031\\N20261201 ~ 20261231",
            }
        )

        self.assertIsNone(record.policy["application_start_date"])
        self.assertIsNone(record.policy["application_end_date"])
        self.assertEqual(
            record.application_periods,
            (
                (date(2026, 10, 1), date(2026, 10, 31)),
                (date(2026, 12, 1), date(2026, 12, 31)),
            ),
        )
        self.assertEqual(
            record.policy["application_period_parse_status"],
            "MULTIPLE",
        )

    def test_transform_policy_rejects_dummy_application_date_but_keeps_raw(self):
        record = load_policies.transform_policy(
            {"plcyNo": "P004", "aplyYmd": "00010101 ~ 00010101"}
        )

        self.assertIsNone(record.policy["application_start_date"])
        self.assertIsNone(record.policy["application_end_date"])
        self.assertEqual(
            record.policy["application_period_raw"],
            "00010101 ~ 00010101",
        )
        self.assertEqual(record.application_periods, ())
        self.assertEqual(record.policy["application_period_parse_status"], "INVALID")

    def test_transform_policy_rejects_reversed_application_date(self):
        record = load_policies.transform_policy(
            {"plcyNo": "P005", "aplyYmd": "20260601 ~ 20250710"}
        )

        self.assertIsNone(record.policy["application_start_date"])
        self.assertIsNone(record.policy["application_end_date"])
        self.assertEqual(record.application_periods, ())
        self.assertEqual(record.policy["application_period_parse_status"], "INVALID")

    def test_transform_policy_does_not_partially_accept_malformed_periods(self):
        record = load_policies.transform_policy(
            {
                "plcyNo": "P006",
                "aplyYmd": "20260101 ~ 20260131\\N추가 접수",
            }
        )

        self.assertIsNone(record.policy["application_start_date"])
        self.assertIsNone(record.policy["application_end_date"])
        self.assertEqual(record.application_periods, ())
        self.assertEqual(record.policy["application_period_parse_status"], "RAW")


class ExtractPageTests(unittest.TestCase):
    def test_extract_page_accepts_normal_empty_list(self) -> None:
        items, total_count = load_policies.extract_page(
            {
                "result": {
                    "pagging": {"totCount": "0"},
                    "youthPolicyList": [],
                }
            }
        )

        self.assertEqual(items, [])
        self.assertEqual(total_count, 0)

    def test_extract_page_reads_items_and_total_count(self) -> None:
        items = [{"plcyNo": "P001"}]

        extracted, total_count = load_policies.extract_page(
            {
                "result": {
                    "pagging": {"totCount": "2,990"},
                    "youthPolicyList": items,
                }
            }
        )

        self.assertEqual(extracted, items)
        self.assertEqual(total_count, 2990)

    def test_extract_page_rejects_missing_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing 'result'"):
            load_policies.extract_page({"error": "temporary failure"})

    def test_extract_page_rejects_missing_policy_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "result.youthPolicyList"):
            load_policies.extract_page({"result": {"error": "invalid key"}})

    def test_extract_page_rejects_wrong_result_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "'result' must be an object"):
            load_policies.extract_page({"result": []})

    def test_extract_page_rejects_wrong_policy_list_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "youthPolicyList' must be a list"):
            load_policies.extract_page(
                {"result": {"youthPolicyList": {"plcyNo": "P001"}}}
            )


class FetchPageResilienceTests(unittest.TestCase):
    @staticmethod
    def response(
        status_code: int,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> MagicMock:
        response = MagicMock()
        response.status_code = status_code
        response.headers = headers or {}
        response.json.return_value = payload if payload is not None else {
            "result": {"pagging": {"totCount": "1"}, "youthPolicyList": []}
        }
        return response

    def test_403_is_not_retried_and_never_exposes_api_key_or_url(self) -> None:
        secret = "VERY_SECRET_KEY"
        api_url = f"https://example.invalid/policies?legacyKey={secret}"
        response = self.response(403)

        with (
            patch.object(load_policies, "API_URL", api_url),
            patch.object(load_policies, "API_KEY", secret),
            patch.object(load_policies.requests, "get", return_value=response) as get,
            patch.object(load_policies.time, "sleep") as sleep,
            self.assertRaises(RuntimeError) as raised,
        ):
            load_policies.fetch_page(15, max_attempts=3)

        message = str(raised.exception)
        formatted_trace = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(message, "Youth API page 15 failed with HTTP 403")
        self.assertNotIn(secret, message)
        self.assertNotIn(api_url, message)
        self.assertNotIn(secret, formatted_trace)
        self.assertNotIn(api_url, formatted_trace)
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_network_error_retries_with_bounded_backoff(self) -> None:
        success = self.response(
            200,
            {"result": {"pagging": {"totCount": "1"}, "youthPolicyList": []}},
        )

        with (
            patch.object(load_policies, "API_URL", "https://example.invalid/api"),
            patch.object(load_policies, "API_KEY", "secret"),
            patch.object(
                load_policies.requests,
                "get",
                side_effect=[
                    load_policies.requests.ConnectionError(
                        "full request URL containing secret"
                    ),
                    success,
                ],
            ) as get,
            patch.object(load_policies.time, "sleep") as sleep,
        ):
            payload = load_policies.fetch_page(
                2,
                max_attempts=2,
                retry_base_delay_seconds=0.5,
            )

        self.assertIn("result", payload)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_exhausted_network_error_trace_is_sanitized(self) -> None:
        secret = "NETWORK_SECRET"

        with (
            patch.object(load_policies, "API_URL", "https://example.invalid/api"),
            patch.object(load_policies, "API_KEY", secret),
            patch.object(
                load_policies.requests,
                "get",
                side_effect=load_policies.requests.ConnectionError(
                    f"GET https://example.invalid/?apiKeyNm={secret}"
                ),
            ),
            patch.object(load_policies.time, "sleep"),
            self.assertRaises(RuntimeError) as raised,
        ):
            load_policies.fetch_page(9, max_attempts=2)

        formatted_trace = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(
            str(raised.exception),
            "Youth API page 9 failed after 2 attempts: network error",
        )
        self.assertNotIn(secret, formatted_trace)
        self.assertNotIn("apiKeyNm", formatted_trace)

    def test_429_retry_after_is_honored(self) -> None:
        limited = self.response(429, headers={"Retry-After": "7"})
        success = self.response(200)

        with (
            patch.object(load_policies, "API_URL", "https://example.invalid/api"),
            patch.object(load_policies, "API_KEY", "secret"),
            patch.object(
                load_policies.requests,
                "get",
                side_effect=[limited, success],
            ),
            patch.object(load_policies.time, "sleep") as sleep,
        ):
            load_policies.fetch_page(3, max_attempts=2)

        sleep.assert_called_once_with(7.0)

    def test_5xx_retry_delay_is_capped_at_30_seconds(self) -> None:
        unavailable = self.response(503)

        with (
            patch.object(load_policies, "API_URL", "https://example.invalid/api"),
            patch.object(load_policies, "API_KEY", "secret"),
            patch.object(
                load_policies.requests,
                "get",
                return_value=unavailable,
            ) as get,
            patch.object(load_policies.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "page 4 failed with HTTP 503"),
        ):
            load_policies.fetch_page(
                4,
                max_attempts=3,
                retry_base_delay_seconds=20,
            )

        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [20, 30.0])

    def test_invalid_json_error_is_sanitized(self) -> None:
        secret = "DO_NOT_LEAK"
        response = self.response(200)
        response.json.side_effect = ValueError(
            f"decode failed at https://example.invalid/?apiKeyNm={secret}"
        )

        with (
            patch.object(load_policies, "API_URL", "https://example.invalid/api"),
            patch.object(load_policies, "API_KEY", secret),
            patch.object(load_policies.requests, "get", return_value=response),
            self.assertRaises(RuntimeError) as raised,
        ):
            load_policies.fetch_page(5)

        message = str(raised.exception)
        self.assertEqual(message, "Youth API page 5 returned invalid JSON")
        self.assertNotIn(secret, message)

    def test_page_delay_is_configurable(self) -> None:
        with patch.object(load_policies.time, "sleep") as sleep:
            load_policies.wait_before_next_page(1.25)

        sleep.assert_called_once_with(1.25)

    def test_negative_page_delay_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            load_policies.wait_before_next_page(-1)


class SyncLifecycleTests(unittest.TestCase):
    def test_complete_sync_requires_exact_consistent_unique_successes(self) -> None:
        complete, reason = load_policies.evaluate_full_sync(
            full_scan_completed=True,
            api_total_count=3,
            totals_consistent=True,
            processed_count=3,
            success_count=3,
            failed_count=0,
            unique_success_count=3,
        )

        self.assertTrue(complete)
        self.assertIsNone(reason)

    def test_failed_policy_prevents_deactivation(self) -> None:
        complete, reason = load_policies.evaluate_full_sync(
            full_scan_completed=True,
            api_total_count=3,
            totals_consistent=True,
            processed_count=3,
            success_count=2,
            failed_count=1,
            unique_success_count=2,
        )

        self.assertFalse(complete)
        self.assertIn("1 policies failed", reason)
        self.assertIn("success count 2 != API total 3", reason)

    def test_truncated_or_changed_total_prevents_deactivation(self) -> None:
        complete, reason = load_policies.evaluate_full_sync(
            full_scan_completed=False,
            api_total_count=100,
            totals_consistent=False,
            processed_count=50,
            success_count=50,
            failed_count=0,
            unique_success_count=50,
        )

        self.assertFalse(complete)
        self.assertIn("did not reach a normal end", reason)
        self.assertIn("changed between pages", reason)
        self.assertIn("processed count 50 != API total 100", reason)

    def test_empty_api_response_never_deactivates_everything(self) -> None:
        complete, reason = load_policies.evaluate_full_sync(
            full_scan_completed=True,
            api_total_count=0,
            totals_consistent=True,
            processed_count=0,
            success_count=0,
            failed_count=0,
            unique_success_count=0,
        )

        self.assertFalse(complete)
        self.assertIn("missing or empty", reason)

    def test_duplicate_policy_numbers_prevent_deactivation(self) -> None:
        complete, reason = load_policies.evaluate_full_sync(
            full_scan_completed=True,
            api_total_count=2,
            totals_consistent=True,
            processed_count=2,
            success_count=2,
            failed_count=0,
            unique_success_count=1,
        )

        self.assertFalse(complete)
        self.assertIn("unique policy count 1 != success count 2", reason)

    def test_api_load_marks_seen_policy_active_for_current_job(self) -> None:
        cursor = MagicMock()
        sync_job_id = uuid4()
        record = load_policies.PolicyRecord(
            policy={"policy_no": "P001"},
            regions=[],
            eligibility_codes=[],
            application_periods=(),
        )

        load_policies.load_policy(cursor, record, sync_job_id)

        lifecycle_calls = [
            call
            for call in cursor.execute.call_args_list
            if "last_seen_sync_job_id" in call.args[0]
        ]
        self.assertEqual(len(lifecycle_calls), 1)
        self.assertIn("is_active = TRUE", lifecycle_calls[0].args[0])
        self.assertIn("deactivated_at = NULL", lifecycle_calls[0].args[0])
        self.assertEqual(lifecycle_calls[0].args[1], (sync_job_id, "P001"))

    def test_deactivate_unseen_policies_is_a_soft_delete(self) -> None:
        cursor = MagicMock()
        cursor.rowcount = 44
        sync_job_id = uuid4()

        count = load_policies.deactivate_unseen_policies(cursor, sync_job_id)

        self.assertEqual(count, 44)
        sql, params = cursor.execute.call_args.args
        self.assertIn("SET is_active = FALSE", sql)
        self.assertIn("last_seen_sync_job_id IS DISTINCT FROM", sql)
        self.assertNotIn("DELETE FROM policy", sql)
        self.assertEqual(params, (sync_job_id,))


class CommandLineTests(unittest.TestCase):
    def test_parser_accepts_positive_page_size(self) -> None:
        args = load_policies.build_arg_parser().parse_args(
            ["api", "--page-size", "50"]
        )

        self.assertEqual(args.command, "api")
        self.assertEqual(args.page_size, 50)

    def test_parser_rejects_non_positive_page_size(self) -> None:
        parser = load_policies.build_arg_parser()

        with (
            patch("sys.stderr", new=io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(["api", "--page-size", "0"])

    def test_run_api_etl_rejects_non_positive_page_size_before_db_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            load_policies.run_api_etl(0)

    def test_main_dispatches_file_command(self) -> None:
        with patch.object(load_policies, "run_json_file", return_value=1) as mocked:
            exit_code = load_policies.main(["file", "sample.json"])

        self.assertEqual(exit_code, 0)
        mocked.assert_called_once_with("sample.json")

    def test_main_dispatches_api_command(self) -> None:
        with (
            patch.object(load_policies, "run_api_etl", return_value=(3, 1)) as mocked,
            patch("sys.stdout", new=io.StringIO()),
        ):
            exit_code = load_policies.main(["api", "--page-size", "25"])

        self.assertEqual(exit_code, 0)
        mocked.assert_called_once_with(25)

    def test_main_dispatches_period_backfill(self) -> None:
        with (
            patch.object(
                load_policies,
                "backfill_application_periods",
                return_value=(10, 8, 2),
            ) as mocked,
            patch("sys.stdout", new=io.StringIO()),
        ):
            exit_code = load_policies.main(["backfill-periods"])

        self.assertEqual(exit_code, 0)
        mocked.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
