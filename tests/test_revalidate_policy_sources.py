from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from etl.revalidate_policy_sources import (
    CURRENT_AI_VALIDATOR_VERSION,
    RevalidationCandidate,
    STALE_AI_VALIDATOR_VERSION,
    StaleAiReviewTarget,
    extracted_page_from_candidate,
    invalidate_stale_ai_review,
    load_stale_ai_review_targets,
    load_revalidation_candidates,
    parse_args,
)
from etl.source_relevance import PolicyEvidence, VALIDATOR_VERSION
from etl.verify_ai_evidence import policy_evidence_hash


class RecordingCursor:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.executions: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self, rows) -> None:
        self.recording_cursor = RecordingCursor(rows)

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor


class SequentialRecordingCursor(RecordingCursor):
    def __init__(self, responses) -> None:
        super().__init__([])
        self.responses = list(responses)
        self.current = []

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        self.current = self.responses.pop(0) if self.responses else []

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)


class SequentialRecordingConnection:
    def __init__(self, responses) -> None:
        self.recording_cursor = SequentialRecordingCursor(responses)

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor


class RevalidationSelectionTests(unittest.TestCase):
    def test_selects_pending_old_validator_or_changed_document(self) -> None:
        conn = RecordingConnection([])

        candidates = load_revalidation_candidates(conn, limit=25)

        self.assertEqual(candidates, [])
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("link.validation_status = 'PENDING'", sql)
        self.assertIn("link.validator_version IS NULL", sql)
        self.assertIn("link.validator_version NOT IN (%s, %s, %s)", sql)
        self.assertIn(
            "link.validated_document_id IS DISTINCT FROM document.document_id",
            sql,
        )
        self.assertIn("document.is_current = TRUE", sql)
        self.assertEqual(
            params,
            (
                VALIDATOR_VERSION,
                CURRENT_AI_VALIDATOR_VERSION,
                STALE_AI_VALIDATOR_VERSION,
                25,
            ),
        )

    def test_current_base_ai_versions_are_not_selected_as_stale(self) -> None:
        self.assertEqual(
            CURRENT_AI_VALIDATOR_VERSION,
            f"{VALIDATOR_VERSION}+ai-evidence-code-v2",
        )

    def test_applied_ai_drift_is_filtered_by_current_policy_hash(self) -> None:
        policy = PolicyEvidence(
            policy_no="P-1",
            policy_name="인천 청년 지원",
            organization_names=("인천청년센터",),
            region_names=("인천광역시",),
            application_periods=("2026-01-01 ~ 2026-12-31",),
            distinctive_terms=("면접비",),
        )
        common = {
            "source_id": "source-1",
            "document_id": "document-1",
            "content_hash": "a" * 64,
            "proposed_status": "VERIFIED",
            "policy_no": policy.policy_no,
            "policy_name": policy.policy_name,
            "organization_names": list(policy.organization_names),
            "region_names": list(policy.region_names),
            "application_periods": list(policy.application_periods),
            "distinctive_terms": list(policy.distinctive_terms),
        }
        unchanged = {
            **common,
            "ai_review_id": "review-unchanged",
            "observed_policy_evidence_hash": policy_evidence_hash(policy),
        }
        stale = {
            **common,
            "ai_review_id": "review-stale",
            "source_id": "source-2",
            "document_id": "document-2",
            "observed_policy_evidence_hash": "0" * 64,
        }
        conn = RecordingConnection([unchanged, stale])

        targets = load_stale_ai_review_targets(conn, limit=10)

        self.assertEqual([target.ai_review_id for target in targets], ["review-stale"])
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("review.applied_at IS NOT NULL", sql)
        self.assertIn("link.validator_version = %s", sql)
        self.assertEqual(params, (CURRENT_AI_VALIDATOR_VERSION,))

    def test_stale_ai_invalidation_updates_all_locked_relation_types(self) -> None:
        policy_row = {
            "policy_no": "P-1",
            "policy_name": "변경된 정책명",
            "organization_names": ["인천청년센터"],
            "region_names": ["인천광역시"],
            "application_periods": ["2026-01-01 ~ 2026-12-31"],
            "distinctive_terms": ["면접비"],
        }
        target = StaleAiReviewTarget(
            ai_review_id="review-1",
            policy_no="P-1",
            source_id="source-1",
            document_id="document-1",
            content_hash="a" * 64,
            proposed_status="VERIFIED",
            observed_policy_evidence_hash="0" * 64,
            current_policy_evidence_hash="1" * 64,
        )
        links = [
            {
                "relation_type": "REFERENCE_1",
                "validation_status": "VERIFIED",
                "validated_document_id": "document-1",
                "validator_version": f"{VALIDATOR_VERSION}+ai-evidence-code-v2",
                "reviewed_at": None,
            }
        ]
        conn = SequentialRecordingConnection(
            [
                [policy_row],
                [{"source_id": "source-1"}],
                [{"document_id": "document-1"}],
                links,
                [{"relation_type": "REFERENCE_1"}],
            ]
        )

        updated = invalidate_stale_ai_review(conn, target=target)

        self.assertEqual(updated, 1)
        update_sql = conn.recording_cursor.executions[-1][0]
        self.assertIn("validation_status = 'REVIEW_REQUIRED'", update_sql)
        self.assertIn("ai_policy_evidence_stale", update_sql)

    def test_rejects_non_positive_limit(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--limit", "0"])

    def test_rebuild_preserves_iframe_retrieval_provenance(self) -> None:
        retrieval = {
            "mode": "NAVER_POST_IFRAME",
            "depth": 1,
            "container_host": "blog.naver.com",
            "container_path": "/example/123",
        }
        candidate = RevalidationCandidate(
            source_id="source-1",
            document_id="document-1",
            final_url="https://blog.naver.com/example/123",
            http_status=200,
            title="정책 제목",
            extracted_text="정책 본문",
            metadata={
                "h1": "정책 제목",
                "headings": ["지원 대상"],
                "retrieval": retrieval,
            },
            policy_nos=("P-1",),
        )

        extracted = extracted_page_from_candidate(candidate)

        self.assertEqual(extracted.retrieval, retrieval)
        self.assertIsNot(extracted.retrieval, retrieval)
        self.assertEqual(extracted.headings, ("지원 대상",))


if __name__ == "__main__":
    unittest.main()
