import unittest
from datetime import date

from etl.generate_policy_chunks import (
    CHUNKER_VERSION,
    _load_policies,
    build_policy_chunks,
    build_region_catalog,
    compress_regions,
    content_hash,
    normalize_text,
)


class RecordingCursor:
    def __init__(self):
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return []


class RecordingConnection:
    def __init__(self):
        self.recording_cursor = RecordingCursor()

    def cursor(self, **kwargs):
        return self.recording_cursor


class PolicySelectionTests(unittest.TestCase):
    def test_chunk_generation_selects_only_active_policies(self):
        conn = RecordingConnection()

        rows = _load_policies(conn, ["P1"], 10)

        self.assertEqual(rows, [])
        self.assertIn("is_active = TRUE", conn.recording_cursor.sql)
        self.assertIn("policy_no = ANY(%s)", conn.recording_cursor.sql)
        self.assertEqual(conn.recording_cursor.params, [["P1"], 10])


class NormalizeTextTests(unittest.TestCase):
    def test_normalizes_line_endings_literal_null_and_invisible_chars(self):
        value = "\ufeff 첫 줄\r\n\r\n\u200b\r\n둘째\\N 셋째 "

        self.assertEqual(normalize_text(value), "첫 줄\n\n둘째\n셋째")

    def test_preserves_semantic_angle_brackets(self):
        self.assertEqual(normalize_text("<생애최초> 대상"), "<생애최초> 대상")


class RegionCompressionTests(unittest.TestCase):
    def setUp(self):
        self.master = [
            {
                "region_code": "11001",
                "sido_code": "11",
                "sido_name": "서울특별시",
                "region_name": "서울특별시 가구",
            },
            {
                "region_code": "11002",
                "sido_code": "11",
                "sido_name": "서울특별시",
                "region_name": "서울특별시 나구",
            },
            {
                "region_code": "26001",
                "sido_code": "26",
                "sido_name": "부산광역시",
                "region_name": "부산광역시 다구",
            },
            {
                "region_code": "26002",
                "sido_code": "26",
                "sido_name": "부산광역시",
                "region_name": "부산광역시 라구",
            },
        ]
        self.catalog = build_region_catalog(self.master)

    def _resolved(self, *codes):
        by_code = {row["region_code"]: row for row in self.master}
        return [
            {
                **by_code[code],
                "resolved_region_code": code,
            }
            for code in codes
        ]

    def test_all_active_regions_become_nationwide(self):
        rows = self._resolved("11001", "11002", "26001", "26002")
        self.assertEqual(compress_regions(self.catalog, rows), ["전국"])

    def test_full_sido_and_partial_regions_are_combined(self):
        rows = self._resolved("26001", "11002", "11001", "11001")
        labels = compress_regions(self.catalog, rows[:-1])

        self.assertEqual(labels, ["서울특별시 전체", "부산광역시 다구"])

        partial = self._resolved("11002", "11002", "26001")
        self.assertEqual(
            compress_regions(self.catalog, partial),
            ["서울특별시 나구", "부산광역시 다구"],
        )


class ChunkRenderingTests(unittest.TestCase):
    def base_policy(self):
        return {
            "policy_no": "P1",
            "policy_name": "청년 지원 정책",
            "description": "청년의 자립을 지원합니다.",
            "support_content": "월 10만원 지원",
            "keywords": ["청년", "자립"],
            "large_categories": ["복지"],
            "middle_categories": ["생활지원"],
            "provider_group_code": "PROVIDER",
            "provision_method_code": "METHOD",
            "approval_status_code": None,
            "supervising_org_name": "청년부",
            "operating_org_name": "지원센터",
            "support_scale_limit": True,
            "support_scale_count": 100,
            "first_come_first_served": False,
            "min_age": 19,
            "max_age": 34,
            "age_limit_yn": True,
            "marriage_status_code": "MARRIAGE",
            "income_condition_code": "INCOME",
            "min_income": None,
            "max_income": 5000,
            "income_etc": None,
            "application_period_type_code": None,
            "application_start_date": None,
            "application_end_date": None,
            "application_period_raw": None,
            "application_period_parse_status": "EMPTY",
            "business_period_type_code": "BIZ",
            "business_start_date": date(2026, 1, 1),
            "business_end_date": date(2026, 12, 31),
            "business_period_etc": None,
            "application_method": None,
            "screening_method": None,
            "submission_documents": None,
            "additional_notes": "<예정> 내용은 변경될 수 있음",
            "additional_qualification": "인천 거주자",
            "participation_exclusion": None,
            "application_url": None,
            "reference_url_1": None,
        }

    def code_lookup(self):
        return {
            ("pvsnInstGroupCd", "PROVIDER"): "중앙부처",
            ("plcyPvsnMthdCd", "METHOD"): "보조금",
            ("mrgSttsCd", "MARRIAGE"): "제한없음",
            ("earnCndSeCd", "INCOME"): "기준중위소득 150% 이하",
            ("bizPrdSeCd", "BIZ"): "연간",
            ("plcyMajorCd", "MAJOR1"): "전공 제한 없음",
        }

    def test_fixed_indices_and_empty_application_is_omitted(self):
        chunks = build_policy_chunks(
            self.base_policy(),
            ["인천광역시 전체"],
            {"MAJOR": ["MAJOR1"]},
            self.code_lookup(),
        )

        self.assertEqual(
            [(chunk.chunk_index, chunk.chunk_type) for chunk in chunks],
            [(0, "SUMMARY"), (1, "ELIGIBILITY"), (2, "SUPPORT")],
        )
        self.assertTrue(
            all(chunk.content.startswith("정책명: 청년 지원 정책") for chunk in chunks)
        )
        combined = "\n".join(chunk.content for chunk in chunks)
        self.assertNotIn("https://", combined)
        self.assertIn("<예정>", combined)
        self.assertIn("적용 지역: 인천광역시 전체", combined)

    def test_application_uses_fixed_index_and_normalizes_literal_null(self):
        policy = self.base_policy()
        policy["application_period_raw"] = "20260101 ~ 20260131\\N추가 접수"
        policy["application_method"] = "온라인"

        chunks = build_policy_chunks(policy, [], {}, self.code_lookup())
        application = next(
            chunk for chunk in chunks if chunk.chunk_type == "APPLICATION"
        )

        self.assertEqual(application.chunk_index, 3)
        self.assertIn("20260101 ~ 20260131\n추가 접수", application.content)

    def test_application_period_type_alone_does_not_create_chunk(self):
        policy = self.base_policy()
        policy["application_period_type_code"] = "PERIOD_TYPE"
        lookup = {**self.code_lookup(), ("aplyPrdSeCd", "PERIOD_TYPE"): "마감"}

        chunks = build_policy_chunks(policy, [], {}, lookup)

        self.assertNotIn("APPLICATION", [chunk.chunk_type for chunk in chunks])

    def test_invalid_application_period_raw_is_not_embedded(self):
        policy = self.base_policy()
        policy["application_period_raw"] = "00010101 ~ 00010101"
        policy["application_period_parse_status"] = "INVALID"
        policy["application_method"] = "온라인"

        chunks = build_policy_chunks(policy, [], {}, self.code_lookup())
        application = next(
            chunk for chunk in chunks if chunk.chunk_type == "APPLICATION"
        )

        self.assertNotIn("00010101", application.content)
        self.assertIn("신청 방법: 온라인", application.content)

    def test_url_is_presence_signal_but_is_not_embedded(self):
        policy = self.base_policy()
        policy["application_period_type_code"] = "PERIOD_TYPE"
        policy["application_url"] = "https://secret.example/apply"
        lookup = {**self.code_lookup(), ("aplyPrdSeCd", "PERIOD_TYPE"): "상시"}

        chunks = build_policy_chunks(policy, [], {}, lookup)
        application = next(
            chunk for chunk in chunks if chunk.chunk_type == "APPLICATION"
        )

        self.assertIn("신청 기간 구분: 상시", application.content)
        self.assertNotIn("https://", application.content)

    def test_hash_is_deterministic_and_changes_with_content(self):
        first = content_hash("같은 내용")
        self.assertEqual(first, content_hash("같은 내용"))
        self.assertNotEqual(first, content_hash("다른 내용"))
        self.assertEqual(len(first), 64)
        self.assertEqual(CHUNKER_VERSION, "policy-v1")


if __name__ == "__main__":
    unittest.main()
