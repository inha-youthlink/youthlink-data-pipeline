import json
import unittest

from etl.ai_evidence_schema import (
    AI_EVIDENCE_JSON_SCHEMA,
    EvidenceSchemaError,
    parse_ai_evidence,
)


def valid_payload():
    return {
        "page_type": "DETAIL_SINGLE",
        "policy_identity": {
            "policy_numbers": [
                {"value": "P-1", "quote": "정책번호 P-1"},
            ],
            "policy_names": [
                {"value": "청년 주거 지원", "quote": "청년 주거 지원 안내"},
            ],
        },
        "supporting_evidence": [
            {
                "category": "ORGANIZATION",
                "value": "청년센터",
                "quote": "주관기관 청년센터",
            }
        ],
        "conflicts": [],
        "lifecycle_flags": [],
        "other_policy_identities": [],
    }


class AiEvidenceSchemaTests(unittest.TestCase):
    def test_parses_closed_payload_from_json(self):
        evidence = parse_ai_evidence(json.dumps(valid_payload()))

        self.assertEqual(evidence.page_type, "DETAIL_SINGLE")
        self.assertEqual(evidence.policy_identity.policy_numbers[0].value, "P-1")
        self.assertEqual(evidence.supporting_evidence[0].category, "ORGANIZATION")

    def test_json_schema_is_closed_at_root_and_nested_objects(self):
        self.assertFalse(AI_EVIDENCE_JSON_SCHEMA["additionalProperties"])
        self.assertFalse(
            AI_EVIDENCE_JSON_SCHEMA["properties"]["policy_identity"][
                "additionalProperties"
            ]
        )
        self.assertFalse(
            AI_EVIDENCE_JSON_SCHEMA["properties"]["supporting_evidence"][
                "items"
            ]["additionalProperties"]
        )

    def test_rejects_markdown_fenced_json(self):
        with self.assertRaises(EvidenceSchemaError):
            parse_ai_evidence("```json\n{}\n```")

    def test_rejects_unknown_root_key(self):
        payload = valid_payload()
        payload["model_decision"] = "VERIFIED"

        with self.assertRaisesRegex(EvidenceSchemaError, "extra=model_decision"):
            parse_ai_evidence(payload)

    def test_rejects_missing_required_array(self):
        payload = valid_payload()
        del payload["conflicts"]

        with self.assertRaisesRegex(EvidenceSchemaError, "missing=conflicts"):
            parse_ai_evidence(payload)

    def test_rejects_unknown_category(self):
        payload = valid_payload()
        payload["supporting_evidence"][0]["category"] = "AGE"

        with self.assertRaisesRegex(EvidenceSchemaError, "allowed category"):
            parse_ai_evidence(payload)

    def test_rejects_duplicate_json_keys(self):
        raw = json.dumps(valid_payload())
        raw = raw.replace(
            '"page_type": "DETAIL_SINGLE"',
            '"page_type": "DETAIL_SINGLE", "page_type": "UNKNOWN"',
            1,
        )

        with self.assertRaisesRegex(EvidenceSchemaError, "duplicate JSON key"):
            parse_ai_evidence(raw)

    def test_rejects_too_many_items(self):
        payload = valid_payload()
        payload["other_policy_identities"] = [
            {"value": f"P-{index}", "quote": f"정책 P-{index}"}
            for index in range(21)
        ]

        with self.assertRaisesRegex(EvidenceSchemaError, "at most 20"):
            parse_ai_evidence(payload)


if __name__ == "__main__":
    unittest.main()
