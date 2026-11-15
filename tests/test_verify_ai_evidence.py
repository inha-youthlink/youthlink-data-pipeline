import unittest

from etl.ai_evidence_schema import parse_ai_evidence
from etl.source_relevance import PageEvidence, PolicyEvidence
from etl.verify_ai_evidence import (
    REJECTED,
    REVIEW_REQUIRED,
    VERIFIED,
    legacy_policy_evidence_hash,
    policy_evidence_hash,
    policy_evidence_payload,
    verify_ai_evidence,
)


class VerifyAiEvidenceTests(unittest.TestCase):
    def policy(self, **overrides):
        values = {
            "policy_no": "P-2026-1",
            "policy_name": "인천 청년 면접 지원",
            "organization_names": ("인천청년센터",),
            "region_names": ("인천광역시",),
            "application_periods": ("2026-01-01 ~ 2026-12-31",),
            "distinctive_terms": ("면접비",),
        }
        values.update(overrides)
        return PolicyEvidence(**values)

    def page(self, **overrides):
        values = {
            "final_url": "https://example.org/detail/1",
            "title": "인천 청년 면접 지원 | 인천청년센터",
            "h1": "인천 청년 면접 지원",
            "headings": ("지원 내용", "신청 기간"),
            "text": (
                "정책번호 P-2026-1\n"
                "주관기관 인천청년센터\n"
                "지원지역 인천광역시\n"
                "지원항목 면접비\n"
                "신청기간 2026-01-01 ~ 2026-12-31"
            ),
        }
        values.update(overrides)
        return PageEvidence(**values)

    def payload(self, **overrides):
        values = {
            "page_type": "DETAIL_SINGLE",
            "policy_identity": {
                "policy_numbers": [
                    {"value": "P-2026-1", "quote": "정책번호 P-2026-1"}
                ],
                "policy_names": [
                    {
                        "value": "인천 청년 면접 지원",
                        "quote": "인천 청년 면접 지원 | 인천청년센터",
                    }
                ],
            },
            "supporting_evidence": [
                {
                    "category": "ORGANIZATION",
                    "value": "인천청년센터",
                    "quote": "주관기관 인천청년센터",
                },
                {
                    "category": "APPLICATION_PERIOD",
                    "value": "2026-01-01 ~ 2026-12-31",
                    "quote": "신청기간 2026-01-01 ~ 2026-12-31",
                },
            ],
            "conflicts": [],
            "lifecycle_flags": [],
            "other_policy_identities": [],
        }
        values.update(overrides)
        return values

    def verify(self, payload=None, policy=None, page=None):
        return verify_ai_evidence(
            policy or self.policy(),
            page or self.page(),
            parse_ai_evidence(payload or self.payload()),
        )

    def test_verified_requires_stored_exact_identity_and_two_support_categories(self):
        result = self.verify()

        self.assertEqual(result.proposed_status, VERIFIED)
        self.assertEqual(result.identity_result, "EXACT")
        self.assertEqual(result.verification_result, "ACCEPTED")
        self.assertEqual(len(result.supporting_evidence), 2)

    def test_quote_matching_normalizes_unicode_and_whitespace(self):
        payload = self.payload()
        payload["policy_identity"]["policy_names"][0]["quote"] = (
            "인천 청년 면접 지원   |   인천청년센터"
        )
        payload["supporting_evidence"][1]["quote"] = (
            "신청기간 ２０２６－０１－０１ ~ ２０２６－１２－３１"
        )

        result = self.verify(payload)

        self.assertEqual(result.proposed_status, VERIFIED)

    def test_hallucinated_quote_is_rejected_and_cannot_count(self):
        payload = self.payload()
        payload["supporting_evidence"][1]["quote"] = "신청은 매일 가능합니다"

        result = self.verify(payload)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertIn("quote_not_found", {item["reason"] for item in result.rejected_evidence})
        self.assertIn("fewer_than_two_independent_support_categories", result.blockers)
        self.assertNotIn("unverified_model_evidence", result.blockers)

    def test_extra_unverified_support_does_not_veto_complete_proof(self):
        payload = self.payload()
        payload["supporting_evidence"].append(
            {
                "category": "REGION",
                "value": "인천광역시",
                "quote": "페이지에 존재하지 않는 지역 인용",
            }
        )

        result = self.verify(payload)

        self.assertEqual(result.proposed_status, VERIFIED)
        self.assertIn(
            "quote_not_found",
            {item["reason"] for item in result.rejected_evidence},
        )
        self.assertNotIn("unverified_model_evidence", result.blockers)

    def test_any_extra_unverified_model_evidence_blocks_approval(self):
        payload = self.payload()
        payload["other_policy_identities"] = [
            {"value": "OTHER-1", "quote": "존재하지 않는 OTHER-1"}
        ]

        result = self.verify(payload)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertIn("unverified_model_evidence", result.reason)

    def test_quote_must_contain_claimed_value(self):
        payload = self.payload()
        payload["supporting_evidence"][1]["quote"] = "지원항목 면접비"

        result = self.verify(payload)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertIn("value_not_in_quote", {item["reason"] for item in result.rejected_evidence})

    def test_policy_value_must_match_api_owned_evidence(self):
        payload = self.payload()
        payload["supporting_evidence"][0] = {
            "category": "ORGANIZATION",
            "value": "부산청년센터",
            "quote": "주관기관 부산청년센터",
        }
        page = self.page(text=self.page().text + "\n주관기관 부산청년센터")

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertIn("policy_value_mismatch", {item["reason"] for item in result.rejected_evidence})

    def test_exact_policy_number_without_exact_name_is_not_enough(self):
        payload = self.payload()
        payload["policy_identity"]["policy_names"] = []

        result = self.verify(payload)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(result.identity_result, "INSUFFICIENT")
        self.assertIn("identity_number_only", result.reason)

    def test_policy_name_only_in_body_is_not_single_policy_identity(self):
        payload = self.payload()
        payload["policy_identity"]["policy_numbers"] = []
        payload["policy_identity"]["policy_names"][0]["quote"] = (
            "인천 청년 면접 지원 등 다양한 정책"
        )
        page = self.page(
            title="청년정책 전체 목록",
            h1="정책 목록",
            text=(
                "인천 청년 면접 지원 등 다양한 정책\n"
                "주관기관 인천청년센터\n"
                "신청기간 2026-01-01 ~ 2026-12-31"
            ),
        )

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(result.identity_result, "INSUFFICIENT")
        self.assertIn("identity_name_body_only", result.reason)

    def test_split_headings_cannot_synthesize_an_exact_policy_name(self):
        payload = self.payload()
        payload["policy_identity"]["policy_numbers"] = []
        payload["policy_identity"]["policy_names"][0]["quote"] = (
            "인천 청년 면접 지원"
        )
        page = self.page(
            title="정책 목록",
            h1="인천 청년",
            headings=("면접 지원", "신청 기간"),
            text=(
                "인천 청년 면접 지원\n"
                "주관기관 인천청년센터\n"
                "신청기간 2026-01-01 ~ 2026-12-31"
            ),
        )

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(result.identity_result, "INSUFFICIENT")

    def test_multi_policy_page_never_auto_verifies(self):
        payload = self.payload(
            page_type="LISTING_MULTI",
            other_policy_identities=[
                {"value": "부산 청년 월세 지원", "quote": "부산 청년 월세 지원"}
            ],
        )
        page = self.page(text=self.page().text + "\n부산 청년 월세 지원")

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(result.identity_result, "CONFLICT")

    def test_lifecycle_warning_blocks_verification(self):
        payload = self.payload(
            lifecycle_flags=[
                {"category": "ENDED", "quote": "본 사업은 종료되었습니다"}
            ]
        )
        page = self.page(text=self.page().text + "\n본 사업은 종료되었습니다")

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(result.lifecycle_flags[0]["category"], "ENDED")

    def test_same_literal_org_and_region_are_one_independent_category(self):
        policy = self.policy(
            organization_names=("인천광역시",),
            region_names=("인천광역시",),
            application_periods=(),
            distinctive_terms=(),
        )
        payload = self.payload(
            supporting_evidence=[
                {
                    "category": "ORGANIZATION",
                    "value": "인천광역시",
                    "quote": "주관기관 인천광역시",
                },
                {
                    "category": "REGION",
                    "value": "인천광역시",
                    "quote": "지원지역 인천광역시",
                },
            ]
        )
        page = self.page(text=self.page().text + "\n주관기관 인천광역시")

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)

    def test_unique_short_region_name_can_match_full_policy_region(self):
        policy = self.policy(
            region_names=("제주특별자치도 제주시", "제주특별자치도 서귀포시"),
        )
        payload = self.payload(
            supporting_evidence=[
                {
                    "category": "REGION",
                    "value": "제주시",
                    "quote": "지원지역 제주시",
                },
                {
                    "category": "APPLICATION_PERIOD",
                    "value": "2026-01-01 ~ 2026-12-31",
                    "quote": "신청기간 2026-01-01 ~ 2026-12-31",
                },
            ]
        )
        page = self.page(text=self.page().text + "\n지원지역 제주시")

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, VERIFIED)

    def test_full_policy_region_can_be_grounded_by_unique_printed_suffix(self):
        policy = self.policy(
            region_names=("제주특별자치도 제주시", "제주특별자치도 서귀포시"),
        )
        payload = self.payload(
            supporting_evidence=[
                {
                    "category": "REGION",
                    "value": "제주특별자치도 제주시",
                    "quote": "지원지역 제주시",
                },
                {
                    "category": "APPLICATION_PERIOD",
                    "value": "2026-01-01 ~ 2026-12-31",
                    "quote": "신청기간 2026-01-01 ~ 2026-12-31",
                },
            ]
        )
        page = self.page(text=self.page().text + "\n지원지역 제주시")

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, VERIFIED)

    def test_full_regions_with_same_printed_suffix_are_ambiguous(self):
        policy = self.policy(
            region_names=("서울특별시 중구", "대전광역시 중구"),
        )
        payload = self.payload(
            supporting_evidence=[
                {
                    "category": "REGION",
                    "value": "서울특별시 중구",
                    "quote": "지원지역 중구",
                },
                {
                    "category": "APPLICATION_PERIOD",
                    "value": "2026-01-01 ~ 2026-12-31",
                    "quote": "신청기간 2026-01-01 ~ 2026-12-31",
                },
            ]
        )
        page = self.page(text=self.page().text + "\n지원지역 중구")

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)

    def test_region_abbreviation_shared_by_targets_is_not_support(self):
        policy = self.policy(
            region_names=("서울특별시 중구", "대전광역시 중구"),
        )
        payload = self.payload(
            supporting_evidence=[
                {
                    "category": "REGION",
                    "value": "중구",
                    "quote": "지원지역 중구",
                },
                {
                    "category": "APPLICATION_PERIOD",
                    "value": "2026-01-01 ~ 2026-12-31",
                    "quote": "신청기간 2026-01-01 ~ 2026-12-31",
                },
            ]
        )
        page = self.page(text=self.page().text + "\n지원지역 중구")

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)

    def test_explicit_other_identity_can_only_propose_rejected_when_target_absent(self):
        payload = self.payload(
            policy_identity={"policy_numbers": [], "policy_names": []},
            supporting_evidence=[],
            conflicts=[
                {
                    "category": "POLICY_NAME",
                    "value": "부산 청년 월세 지원",
                    "quote": "부산 청년 월세 지원",
                }
            ],
        )
        page = self.page(
            title="부산 청년 월세 지원",
            h1="부산 청년 월세 지원",
            text="부산 청년 월세 지원 대상 안내",
        )

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REJECTED)
        self.assertEqual(result.verification_result, "ACCEPTED")

    def test_other_identity_label_alone_cannot_propose_rejected(self):
        payload = self.payload(
            policy_identity={"policy_numbers": [], "policy_names": []},
            supporting_evidence=[],
            other_policy_identities=[
                {"value": "부산청년센터", "quote": "문의 부산청년센터"}
            ],
        )
        page = self.page(
            title="청년 지원 안내",
            h1="청년 지원 안내",
            text="문의 부산청년센터",
        )

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)

    def test_other_policy_name_in_body_only_cannot_propose_rejected(self):
        payload = self.payload(
            policy_identity={"policy_numbers": [], "policy_names": []},
            supporting_evidence=[],
            conflicts=[
                {
                    "category": "POLICY_NAME",
                    "value": "부산 청년 월세 지원",
                    "quote": "관련 사업: 부산 청년 월세 지원",
                }
            ],
        )
        page = self.page(
            title="청년 지원 안내",
            h1="청년 지원 안내",
            text="관련 사업: 부산 청년 월세 지원",
        )

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)

    def test_unverified_extra_claim_blocks_rejected_proposal(self):
        payload = self.payload(
            policy_identity={"policy_numbers": [], "policy_names": []},
            supporting_evidence=[],
            conflicts=[
                {
                    "category": "POLICY_NAME",
                    "value": "부산 청년 월세 지원",
                    "quote": "부산 청년 월세 지원",
                }
            ],
            other_policy_identities=[
                {"value": "OTHER-404", "quote": "존재하지 않는 OTHER-404"}
            ],
        )
        page = self.page(
            title="부산 청년 월세 지원",
            h1="부산 청년 월세 지원",
            text="부산 청년 월세 지원 대상 안내",
        )

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertIn("unverified_model_evidence", result.reason)

    def test_target_policy_number_makes_other_name_conflict_ambiguous(self):
        payload = self.payload(
            policy_identity={
                "policy_numbers": [
                    {"value": "P-2026-1", "quote": "정책번호 P-2026-1"}
                ],
                "policy_names": [],
            },
            supporting_evidence=[],
            conflicts=[
                {
                    "category": "POLICY_NAME",
                    "value": "부산 청년 월세 지원",
                    "quote": "부산 청년 월세 지원",
                }
            ],
        )
        page = self.page(
            title="부산 청년 월세 지원",
            h1="부산 청년 월세 지원",
            text="정책번호 P-2026-1\n부산 청년 월세 지원 대상 안내",
        )

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)

    def test_conflicting_org_blocks_but_does_not_reject(self):
        payload = self.payload(
            conflicts=[
                {
                    "category": "ORGANIZATION",
                    "value": "부산청년센터",
                    "quote": "문의 부산청년센터",
                }
            ]
        )
        page = self.page(text=self.page().text + "\n문의 부산청년센터")

        result = self.verify(payload, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(result.conflicting_evidence[0]["category"], "ORGANIZATION")

    def test_shortened_administrative_org_is_reclassified_as_support(self):
        policy = self.policy(
            organization_names=(
                "인천광역시 남동구 정책기획국 일자리정책과",
            )
        )
        payload = self.payload(
            supporting_evidence=[
                {
                    "category": "APPLICATION_PERIOD",
                    "value": "2026-01-01 ~ 2026-12-31",
                    "quote": "신청기간 2026-01-01 ~ 2026-12-31",
                }
            ],
            conflicts=[
                {
                    "category": "ORGANIZATION",
                    "value": "인천광역시 남동구 일자리정책과",
                    "quote": "담당 인천광역시 남동구 일자리정책과",
                }
            ],
        )
        page = self.page(
            text=(
                self.page().text
                + "\n담당 인천광역시 남동구 일자리정책과"
            )
        )

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, VERIFIED)
        self.assertFalse(result.conflicting_evidence)
        self.assertIn(
            "ORGANIZATION",
            {item["category"] for item in result.supporting_evidence},
        )

    def test_shortened_org_does_not_cross_local_scope(self):
        policy = self.policy(
            organization_names=("인천광역시 서구 일자리정책과",)
        )
        payload = self.payload(
            conflicts=[
                {
                    "category": "ORGANIZATION",
                    "value": "인천광역시 서해구 일자리정책과",
                    "quote": "담당 인천광역시 서해구 일자리정책과",
                }
            ]
        )
        page = self.page(
            text=self.page().text + "\n담당 인천광역시 서해구 일자리정책과"
        )

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertIn("conflicting_evidence", result.blockers)

    def test_shortened_org_does_not_cross_metropolitan_scope(self):
        policy = self.policy(
            organization_names=("인천광역시 미래산업국 반도체바이오과",)
        )
        payload = self.payload(
            conflicts=[
                {
                    "category": "ORGANIZATION",
                    "value": "부산광역시 반도체바이오과",
                    "quote": "담당 부산광역시 반도체바이오과",
                }
            ]
        )
        page = self.page(
            text=self.page().text + "\n담당 부산광역시 반도체바이오과"
        )

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertIn("conflicting_evidence", result.blockers)

    def test_long_api_fact_copied_in_one_page_line_adds_code_support(self):
        policy = self.policy(
            support_content="청년에게 면접 준비 비용을 최대 오십만원까지 지원",
        )
        payload = self.payload(
            supporting_evidence=[
                {
                    "category": "ORGANIZATION",
                    "value": "인천청년센터",
                    "quote": "주관기관 인천청년센터",
                }
            ]
        )
        page = self.page(
            text=(
                self.page().text
                + "\n지원내용 청년에게 면접 준비 비용을 최대 오십만원까지 지원"
            )
        )

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, VERIFIED)
        code_support = [
            item for item in result.supporting_evidence
            if item["kind"] == "CODE_SUPPORT"
        ]
        self.assertEqual(code_support[0]["category"], "SUPPORT_CONTENT")
        self.assertIn(code_support[0]["quote"], page.text)

    def test_short_api_text_is_not_independent_code_support(self):
        policy = self.policy(support_content="면접비 지원")
        payload = self.payload(supporting_evidence=[self.payload()["supporting_evidence"][0]])

        result = self.verify(payload, policy=policy)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)

    def test_same_api_fragment_in_two_fields_counts_once(self):
        fact = "청년에게 면접 준비 비용을 최대 오십만원까지 지원"
        policy = self.policy(description=fact, support_content=fact)
        payload = self.payload(supporting_evidence=[])
        page = self.page(text=self.page().text + "\n" + fact)

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(
            len(
                [
                    item for item in result.supporting_evidence
                    if item["kind"] == "CODE_SUPPORT"
                ]
            ),
            1,
        )

    def test_contained_api_fragments_count_once(self):
        description = "청년에게 면접 준비 비용을 최대 오십만원까지 지원"
        support = "면접 준비 비용을 최대 오십만원까지 지원"
        policy = self.policy(
            description=description,
            support_content=support,
        )
        payload = self.payload(supporting_evidence=[])
        page = self.page(text=self.page().text + "\n" + description)

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertEqual(
            len(
                [
                    item for item in result.supporting_evidence
                    if item["kind"] == "CODE_SUPPORT"
                ]
            ),
            1,
        )

    def test_api_fragment_split_across_page_lines_is_not_counted(self):
        fact = "청년에게 면접 준비 비용을 최대 오십만원까지 지원"
        policy = self.policy(support_content=fact)
        payload = self.payload(supporting_evidence=[])
        page = self.page(
            text=self.page().text + "\n청년에게 면접 준비 비용을\n최대 오십만원까지 지원"
        )

        result = self.verify(payload, policy=policy, page=page)

        self.assertEqual(result.proposed_status, REVIEW_REQUIRED)
        self.assertFalse(
            [
                item for item in result.supporting_evidence
                if item["kind"] == "CODE_SUPPORT"
            ]
        )

    def test_policy_evidence_hash_is_stable_and_changes_with_context(self):
        first = self.policy()
        same = self.policy()
        changed = self.policy(distinctive_terms=("교통비",))

        self.assertEqual(policy_evidence_payload(first), policy_evidence_payload(same))
        self.assertEqual(policy_evidence_hash(first), policy_evidence_hash(same))
        self.assertNotEqual(policy_evidence_hash(first), policy_evidence_hash(changed))

    def test_current_hash_tracks_code_only_corroboration_but_legacy_does_not(self):
        first = self.policy()
        changed = self.policy(support_content="청년에게 면접 준비 비용을 지원")

        self.assertNotEqual(policy_evidence_hash(first), policy_evidence_hash(changed))
        self.assertEqual(
            legacy_policy_evidence_hash(first),
            legacy_policy_evidence_hash(changed),
        )


if __name__ == "__main__":
    unittest.main()
