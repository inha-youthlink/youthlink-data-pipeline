from __future__ import annotations

import importlib
import io
import unittest
from datetime import date
from unittest.mock import patch


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
