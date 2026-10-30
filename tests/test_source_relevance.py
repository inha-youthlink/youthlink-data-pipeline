import unittest

from etl.source_relevance import (
    LINK_ONLY,
    REJECTED,
    REVIEW_REQUIRED,
    VALIDATOR_VERSION,
    VERIFIED,
    PageEvidence,
    PolicyEvidence,
    normalize_for_match,
    validate_policy_source,
)


class SourceRelevanceTests(unittest.TestCase):
    def policy(self, **overrides):
        values = {
            "policy_no": "20260001",
            "policy_name": "인천 청년 주택임차보증금 이자지원",
            "organization_names": ("인천광역시",),
            "region_names": ("인천광역시",),
            "application_periods": ("2026년 1월 1일",),
            "distinctive_terms": ("주택임차보증금 이자",),
        }
        values.update(overrides)
        return PolicyEvidence(**values)

    def test_normalizes_korean_spacing_width_and_punctuation(self):
        self.assertEqual(
            normalize_for_match(" 인천－청년  지원! "),
            "인천청년지원",
        )

    def test_policy_number_in_url_alone_requires_review(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://youth.example/policy/20260001",
                title="부산 축제 안내",
                text="정책과 관련 없는 지역 축제 소식입니다.",
            ),
        )

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertIn("policy_no_exact", result.matched_signals)
        self.assertEqual(result.validator_version, VALIDATOR_VERSION)

    def test_exact_policy_number_and_exact_heading_verify(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://youth.example/policy/20260001",
                h1="인천 청년 주택임차보증금 이자지원",
                text="지원 대상과 신청 방법 안내",
            ),
        )

        self.assertEqual(result.status, VERIFIED)
        self.assertIn("policy_no_url_exact", result.matched_signals)
        self.assertIn("policy_name_heading_exact", result.matched_signals)

    def test_policy_number_only_in_body_listing_does_not_auto_verify(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/policy-list",
                title="청년 정책 전체 목록",
                text="20260001 외 100개 정책을 확인할 수 있습니다.",
            ),
        )

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertIn("policy_no_body_exact", result.matched_signals)

    def test_exact_heading_needs_two_independent_supporting_signals(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://youth.example/policy/detail",
                h1="인천청년 주택임차보증금 이자지원",
                text=(
                    "인천광역시가 주택임차보증금 이자를 지원합니다. "
                    "접수는 2026년 1월 1일 시작합니다."
                ),
            ),
        )

        self.assertEqual(result.status, VERIFIED)
        self.assertIn("policy_name_heading_exact", result.matched_signals)
        self.assertIn("organization_match", result.matched_signals)
        self.assertIn("region_match", result.matched_signals)
        self.assertIn("application_period_match", result.matched_signals)

    def test_same_literal_organization_and_region_counts_as_one_signal(self):
        result = validate_policy_source(
            self.policy(application_periods=(), distinctive_terms=()),
            PageEvidence(
                final_url="https://youth.example/policy/detail",
                h1="인천 청년 주택임차보증금 이자지원",
                text="인천광역시에서 시행합니다.",
            ),
        )

        self.assertEqual(result.status, REVIEW_REQUIRED)

    def test_exact_heading_alone_requires_review(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/notice/1",
                title="인천 청년 주택임차보증금 이자지원",
                text="자세한 내용은 첨부파일을 참고하세요.",
            ),
        )

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertLess(result.score, 0.8)

    def test_body_name_similarity_does_not_auto_verify(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/news",
                title="청년 주거정책 모음",
                text="인천 청년 주택임차보증금 이자지원 등 여러 사업을 소개합니다.",
            ),
        )

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertNotEqual(result.status, VERIFIED)

    def test_longer_similar_policy_heading_is_not_an_exact_name_match(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/other-policy",
                h1="인천 청년 주택임차보증금 이자지원 추가모집",
                text=(
                    "인천광역시 주택임차보증금 이자 지원 안내. "
                    "2026년 1월 1일부터 접수합니다."
                ),
            ),
        )

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertNotIn("policy_name_heading_exact", result.matched_signals)

    def test_generic_login_or_application_portal_is_link_only(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://apply.example/login?policyNo=20260001",
                title="온라인 신청시스템 로그인",
                text="아이디와 비밀번호를 입력하세요.",
            ),
        )

        self.assertEqual(result.status, LINK_ONLY)
        self.assertNotEqual(result.status, VERIFIED)

    def test_http_error_page_is_rejected(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/missing",
                title="페이지를 찾을 수 없습니다",
                http_status=404,
            ),
        )

        self.assertEqual(result.status, REJECTED)
        self.assertIn("error_or_missing_page", result.conflicting_signals)

    def test_http_200_soft_404_body_is_rejected(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/policy/20260001",
                title="정책 안내",
                text="요청하신 페이지를 찾을 수 없습니다. 주소를 확인해 주세요.",
                http_status=200,
            ),
        )

        self.assertEqual(result.status, REJECTED)
        self.assertIn("error_or_missing_page", result.conflicting_signals)

    def test_different_declared_policy_is_rejected_even_with_target_heading(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/policy/other",
                title="인천 청년 주택임차보증금 이자지원",
                text="인천광역시 주택임차보증금 이자 지원",
                declared_policy_no="OTHER-999",
                declared_policy_name="부산 청년 월세 지원",
            ),
        )

        self.assertEqual(result.status, REJECTED)
        self.assertIn("declared_policy_no_mismatch", result.conflicting_signals)
        self.assertIn("declared_policy_name_mismatch", result.conflicting_signals)

    def test_explicit_organization_conflict_overrides_name_match(self):
        result = validate_policy_source(
            self.policy(),
            PageEvidence(
                final_url="https://example.org/policy/1",
                h1="인천 청년 주택임차보증금 이자지원",
                text="청년 주택임차보증금 사업",
                organization_names=("부산광역시",),
                region_names=("부산광역시",),
            ),
        )

        self.assertEqual(result.status, REJECTED)
        self.assertIn("organization_conflict", result.conflicting_signals)
        self.assertIn("region_conflict", result.conflicting_signals)

    def test_mapping_inputs_are_supported(self):
        result = validate_policy_source(
            {
                "policy_no": "P-1",
                "policy_name": "청년 취업 지원",
                "operating_org_name": "청년센터",
                "region_names": ["서울특별시"],
                "distinctive_terms": ["면접수당"],
            },
            {
                "final_url": "https://example.org/detail/P-1",
                "h1": "청년 취업 지원",
                "text": "청년센터 면접수당 안내",
            },
        )

        self.assertEqual(result.status, VERIFIED)

    def test_missing_policy_identity_can_never_auto_verify(self):
        result = validate_policy_source(
            {"policy_no": "", "policy_name": ""},
            {"final_url": "https://example.org", "title": "정책"},
        )

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertEqual(result.score, 0.0)


if __name__ == "__main__":
    unittest.main()
