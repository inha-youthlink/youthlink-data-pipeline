from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from etl import load_regions


def sido_feature(code: str, name: str) -> dict:
    return {
        "properties": {
            "ctprvn_cd": code,
            "ctp_kor_nm": name,
        }
    }


def sigungu_feature(code: str, name: str) -> dict:
    return {
        "properties": {
            "sig_cd": code,
            "sig_kor_nm": name,
        }
    }


class FetchAllRegionsTests(unittest.TestCase):
    def test_filters_non_prefix_results_and_deduplicates_same_region(self) -> None:
        with (
            patch.object(
                load_regions,
                "get_sidos",
                return_value=[sido_feature("30", "대전광역시")],
            ),
            patch.object(
                load_regions,
                "get_sigungu",
                return_value=[
                    sigungu_feature("30110", "동구"),
                    sigungu_feature("43130", "충주시"),
                    sigungu_feature("30110", "동구"),
                ],
            ),
            patch("sys.stdout", new=io.StringIO()),
        ):
            regions = load_regions.fetch_all_regions()

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].region_code, "30110")
        self.assertEqual(regions[0].region_name, "대전광역시 동구")

    def test_rejects_conflicting_names_for_the_same_region_code(self) -> None:
        with (
            patch.object(
                load_regions,
                "get_sidos",
                return_value=[sido_feature("30", "대전광역시")],
            ),
            patch.object(
                load_regions,
                "get_sigungu",
                return_value=[
                    sigungu_feature("30110", "동구"),
                    sigungu_feature("30110", "다른 이름"),
                ],
            ),
            patch("sys.stdout", new=io.StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "Conflicting region data"):
                load_regions.fetch_all_regions()


if __name__ == "__main__":
    unittest.main()
