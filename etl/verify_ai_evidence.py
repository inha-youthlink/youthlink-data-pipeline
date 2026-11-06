from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

try:
    from .ai_evidence_schema import (
        AiEvidence,
        CategorizedEvidence,
        LifecycleEvidence,
        QuotedValue,
    )
    from .source_relevance import PageEvidence, PolicyEvidence, normalize_for_match
except ImportError:  # pragma: no cover - direct script/import compatibility
    from ai_evidence_schema import (
        AiEvidence,
        CategorizedEvidence,
        LifecycleEvidence,
        QuotedValue,
    )
    from source_relevance import PageEvidence, PolicyEvidence, normalize_for_match


AI_REVIEWER_VERSION = "ai-evidence-code-v2"
VERIFIED = "VERIFIED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
REJECTED = "REJECTED"

_WHITESPACE_RE = re.compile(r"\s+")


def policy_evidence_payload(
    policy: PolicyEvidence | Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stable API-owned policy context used for one AI review."""
    if not isinstance(policy, PolicyEvidence):
        policy = PolicyEvidence.from_mapping(policy)
    return {
        "policy_no": policy.policy_no,
        "policy_name": policy.policy_name,
        "organization_names": list(policy.organization_names),
        "region_names": list(policy.region_names),
        "application_periods": list(policy.application_periods),
        "distinctive_terms": list(policy.distinctive_terms),
    }


def policy_evidence_hash(
    policy: PolicyEvidence | Mapping[str, Any],
) -> str:
    """Fingerprint policy context so a cached observation cannot hide drift."""
    encoded = json.dumps(
        policy_evidence_payload(policy),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EvidenceVerification:
    proposed_status: str
    proposed_score: float
    page_type: str
    identity_result: str
    identity_evidence: tuple[dict[str, str], ...] = field(default_factory=tuple)
    supporting_evidence: tuple[dict[str, str], ...] = field(default_factory=tuple)
    conflicting_evidence: tuple[dict[str, str], ...] = field(default_factory=tuple)
    lifecycle_flags: tuple[dict[str, str], ...] = field(default_factory=tuple)
    rejected_evidence: tuple[dict[str, str], ...] = field(default_factory=tuple)
    verification_result: str = "INSUFFICIENT"
    reason: str = ""
    reviewer_version: str = AI_REVIEWER_VERSION


def _quote_match_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _compact_match(value: Any) -> str:
    return normalize_for_match(value)


def _quote_occurs(quote: str, page_text: str) -> bool:
    normalized_quote = _quote_match_text(quote)
    return len(normalized_quote) >= 2 and normalized_quote in page_text


def _value_occurs_in_quote(value: str, quote: str) -> bool:
    normalized_value = _compact_match(value)
    normalized_quote = _compact_match(quote)
    return len(normalized_value) >= 2 and normalized_value in normalized_quote


def _same_value(left: str, right: str) -> bool:
    normalized_left = _compact_match(left)
    normalized_right = _compact_match(right)
    return bool(normalized_left) and normalized_left == normalized_right


def _evidence_dict(
    evidence: QuotedValue | CategorizedEvidence | LifecycleEvidence,
    *,
    kind: str | None = None,
) -> dict[str, str]:
    result = {
        "quote": evidence.quote,
    }
    if hasattr(evidence, "category"):
        result["category"] = evidence.category
    if hasattr(evidence, "value"):
        result["value"] = evidence.value
    if kind is not None:
        result["kind"] = kind
    return result


def _rejected(
    evidence: QuotedValue | CategorizedEvidence | LifecycleEvidence,
    *,
    kind: str,
    reason: str,
) -> dict[str, str]:
    return {**_evidence_dict(evidence, kind=kind), "reason": reason}


def _expected_support_values(
    policy: PolicyEvidence,
    category: str,
) -> tuple[str, ...]:
    if category == "ORGANIZATION":
        return policy.organization_names
    if category == "REGION":
        return policy.region_names
    if category == "APPLICATION_PERIOD":
        return policy.application_periods
    if category == "DISTINCTIVE_TERM":
        return policy.distinctive_terms
    return ()


def _expected_conflict_values(
    policy: PolicyEvidence,
    category: str,
) -> tuple[str, ...]:
    if category == "POLICY_NUMBER":
        return (policy.policy_no,)
    if category == "POLICY_NAME":
        return (policy.policy_name,)
    if category == "ORGANIZATION":
        return policy.organization_names
    if category == "REGION":
        return policy.region_names
    return ()


def _matches_one_exact(value: str, expected: Sequence[str]) -> bool:
    return any(_same_value(value, item) for item in expected)


def _matches_support_value(
    value: str,
    expected: Sequence[str],
    category: str,
) -> bool:
    if category != "REGION":
        return _matches_one_exact(value, expected)

    # Stored policy regions use full administrative names while a source may
    # print only the sigungu (for example, "제주시"). Accept that abbreviation
    # only when it maps to exactly one expected region for this target policy.
    # This does not permit a generic prefix shared by multiple target regions.
    normalized_value = _compact_match(value)
    if len(normalized_value) < 2:
        return False
    matches = {
        _compact_match(item)
        for item in expected
        if normalized_value == _compact_match(item)
        or _compact_match(item).endswith(normalized_value)
        or normalized_value.endswith(_compact_match(item))
    }
    matches.discard("")
    return len(matches) == 1


def _region_value_occurs_in_quote(
    value: str,
    quote: str,
    expected: Sequence[str],
) -> bool:
    """Allow a full API region value backed by one unique printed suffix."""
    if _value_occurs_in_quote(value, quote):
        return True
    if not _matches_one_exact(value, expected):
        return False

    parts = str(value).split()
    if len(parts) < 2:
        return False
    suffix = _compact_match(" ".join(parts[1:]))
    if len(suffix) < 2 or suffix not in _compact_match(quote):
        return False

    matching_expected = {
        _compact_match(item)
        for item in expected
        if len(str(item).split()) >= 2
        and _compact_match(" ".join(str(item).split()[1:])) == suffix
    }
    matching_expected.discard("")
    return len(matching_expected) == 1


def _deduplicate(items: Sequence[dict[str, str]]) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for item in items:
        key = tuple(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def verify_ai_evidence(
    policy: PolicyEvidence | Mapping[str, Any],
    page: PageEvidence | Mapping[str, Any],
    evidence: AiEvidence,
) -> EvidenceVerification:
    """Recompute an AI-assisted proposal from quotes in one stored document.

    Model output never directly controls canonical status. Every accepted item
    must be found in the exact stored page, and every value must be compared
    with API-owned policy evidence before it can count toward a proposal.
    """
    if not isinstance(policy, PolicyEvidence):
        policy = PolicyEvidence.from_mapping(policy)
    if not isinstance(page, PageEvidence):
        page = PageEvidence.from_mapping(page)

    combined_page = "\n".join(
        (
            page.title,
            page.h1,
            *page.headings,
            page.text,
        )
    )
    quote_haystack = _quote_match_text(combined_page)
    normalized_headings = tuple(
        _compact_match(heading)
        for heading in (page.title, page.h1, *page.headings)
    )

    identity_evidence: list[dict[str, str]] = []
    supporting_evidence: list[dict[str, str]] = []
    conflicting_evidence: list[dict[str, str]] = []
    lifecycle_flags: list[dict[str, str]] = []
    rejected_evidence: list[dict[str, str]] = []

    exact_number = False
    exact_name = False
    for kind, items, expected in (
        ("POLICY_NUMBER", evidence.policy_identity.policy_numbers, policy.policy_no),
        ("POLICY_NAME", evidence.policy_identity.policy_names, policy.policy_name),
    ):
        for item in items:
            if not _quote_occurs(item.quote, quote_haystack):
                rejected_evidence.append(
                    _rejected(item, kind=kind, reason="quote_not_found")
                )
                continue
            if not _same_value(item.value, expected):
                rejected_evidence.append(
                    _rejected(item, kind=kind, reason="target_value_mismatch")
                )
                continue
            if not _value_occurs_in_quote(item.value, item.quote):
                rejected_evidence.append(
                    _rejected(item, kind=kind, reason="value_not_in_quote")
                )
                continue
            identity_evidence.append(_evidence_dict(item, kind=kind))
            if kind == "POLICY_NUMBER":
                exact_number = True
            else:
                exact_name = True

    other_identity_count = 0
    for item in evidence.other_policy_identities:
        if not _quote_occurs(item.quote, quote_haystack):
            rejected_evidence.append(
                _rejected(item, kind="OTHER_POLICY", reason="quote_not_found")
            )
            continue
        if not _value_occurs_in_quote(item.value, item.quote):
            rejected_evidence.append(
                _rejected(item, kind="OTHER_POLICY", reason="value_not_in_quote")
            )
            continue
        if _same_value(item.value, policy.policy_no) or _same_value(
            item.value, policy.policy_name
        ):
            rejected_evidence.append(
                _rejected(item, kind="OTHER_POLICY", reason="target_is_not_other")
            )
            continue
        conflicting_evidence.append(_evidence_dict(item, kind="OTHER_POLICY"))
        other_identity_count += 1

    for item in evidence.supporting_evidence:
        if not _quote_occurs(item.quote, quote_haystack):
            rejected_evidence.append(
                _rejected(item, kind="SUPPORT", reason="quote_not_found")
            )
            continue
        expected_values = _expected_support_values(policy, item.category)
        value_is_grounded = _value_occurs_in_quote(item.value, item.quote)
        if item.category == "REGION" and not value_is_grounded:
            value_is_grounded = _region_value_occurs_in_quote(
                item.value,
                item.quote,
                expected_values,
            )
        if not value_is_grounded:
            rejected_evidence.append(
                _rejected(item, kind="SUPPORT", reason="value_not_in_quote")
            )
            continue
        if not _matches_support_value(
            item.value,
            expected_values,
            item.category,
        ):
            rejected_evidence.append(
                _rejected(item, kind="SUPPORT", reason="policy_value_mismatch")
            )
            continue
        supporting_evidence.append(_evidence_dict(item, kind="SUPPORT"))

    explicit_heading_name_conflict = False
    for item in evidence.conflicts:
        if not _quote_occurs(item.quote, quote_haystack):
            rejected_evidence.append(
                _rejected(item, kind="CONFLICT", reason="quote_not_found")
            )
            continue
        if not _value_occurs_in_quote(item.value, item.quote):
            rejected_evidence.append(
                _rejected(item, kind="CONFLICT", reason="value_not_in_quote")
            )
            continue
        expected_values = _expected_conflict_values(policy, item.category)
        if not expected_values or _matches_one_exact(item.value, expected_values):
            rejected_evidence.append(
                _rejected(item, kind="CONFLICT", reason="not_a_conflict")
            )
            continue
        conflicting_evidence.append(_evidence_dict(item, kind="CONFLICT"))
        if item.category == "POLICY_NAME":
            normalized_value = _compact_match(item.value)
            if normalized_value and any(
                normalized_value in heading
                for heading in normalized_headings
                if heading
            ):
                explicit_heading_name_conflict = True

    for item in evidence.lifecycle_flags:
        if not _quote_occurs(item.quote, quote_haystack):
            rejected_evidence.append(
                _rejected(item, kind="LIFECYCLE", reason="quote_not_found")
            )
            continue
        lifecycle_flags.append(_evidence_dict(item, kind="LIFECYCLE"))

    identity_evidence_tuple = _deduplicate(identity_evidence)
    supporting_evidence_tuple = _deduplicate(supporting_evidence)
    conflicting_evidence_tuple = _deduplicate(conflicting_evidence)
    lifecycle_flags_tuple = _deduplicate(lifecycle_flags)
    rejected_evidence_tuple = _deduplicate(rejected_evidence)

    # A name copied into a listing/body is not by itself a single-policy
    # identity. Require the exact target name in a heading, or pair an exact
    # body name with the exact policy number.
    normalized_policy_name = _compact_match(policy.policy_name)
    name_in_heading = exact_name and any(
        normalized_policy_name in heading
        for heading in normalized_headings
        if heading
    )
    target_identity_exact = exact_name and (name_in_heading or exact_number)
    multiple_identities = other_identity_count > 0
    if target_identity_exact and not multiple_identities:
        identity_result = "EXACT"
    elif explicit_heading_name_conflict or multiple_identities:
        identity_result = "CONFLICT"
    else:
        identity_result = "INSUFFICIENT"

    support_categories = {
        item["category"] for item in supporting_evidence_tuple
    }
    # A metropolitan government literal may be both organization and region;
    # one phrase is not two independent corroborating facts.
    if {"ORGANIZATION", "REGION"}.issubset(support_categories):
        organization_values = {
            _compact_match(item["value"])
            for item in supporting_evidence_tuple
            if item["category"] == "ORGANIZATION"
        }
        region_values = {
            _compact_match(item["value"])
            for item in supporting_evidence_tuple
            if item["category"] == "REGION"
        }
        if organization_values & region_values:
            support_categories.remove("REGION")

    blocking_conflict = bool(conflicting_evidence_tuple)
    can_verify = (
        evidence.page_type == "DETAIL_SINGLE"
        and identity_result == "EXACT"
        and len(support_categories) >= 2
        and not blocking_conflict
        and not lifecycle_flags_tuple
        and not rejected_evidence_tuple
    )
    if can_verify:
        return EvidenceVerification(
            proposed_status=VERIFIED,
            proposed_score=min(0.99, 0.90 + 0.03 * len(support_categories)),
            page_type=evidence.page_type,
            identity_result=identity_result,
            identity_evidence=identity_evidence_tuple,
            supporting_evidence=supporting_evidence_tuple,
            conflicting_evidence=conflicting_evidence_tuple,
            lifecycle_flags=lifecycle_flags_tuple,
            rejected_evidence=rejected_evidence_tuple,
            verification_result="ACCEPTED",
            reason=(
                "Stored quotes prove the exact target policy and at least two "
                "independent policy facts on a single-policy detail page."
            ),
        )

    # Rejection is deliberately narrower than approval. A model-extracted
    # organization/region difference cannot reject a source. Only a non-target
    # policy name grounded in one individual title/heading on a purported
    # single-policy detail page can form a rejection proposal. A generic
    # `other_policy_identities` value only blocks approval because quote
    # presence alone cannot prove that an arbitrary phrase is a policy name.
    # Even a heading conflict remains an observation until the separate apply
    # command rechecks the exact document transactionally.
    can_reject = (
        evidence.page_type == "DETAIL_SINGLE"
        and not target_identity_exact
        and not exact_number
        and explicit_heading_name_conflict
        and not rejected_evidence_tuple
    )
    if can_reject:
        return EvidenceVerification(
            proposed_status=REJECTED,
            proposed_score=0.0,
            page_type=evidence.page_type,
            identity_result=identity_result,
            identity_evidence=identity_evidence_tuple,
            supporting_evidence=supporting_evidence_tuple,
            conflicting_evidence=conflicting_evidence_tuple,
            lifecycle_flags=lifecycle_flags_tuple,
            rejected_evidence=rejected_evidence_tuple,
            verification_result="ACCEPTED",
            reason="Stored quotes identify a different policy on a single-policy detail page.",
        )

    blockers: list[str] = []
    if evidence.page_type != "DETAIL_SINGLE":
        blockers.append("page_not_single_policy_detail")
    if identity_result != "EXACT":
        if exact_number and not exact_name:
            blockers.append("identity_number_only")
        elif exact_name and not target_identity_exact:
            blockers.append("identity_name_body_only")
        else:
            blockers.append(f"identity_{identity_result.casefold()}")
    if len(support_categories) < 2:
        blockers.append("fewer_than_two_independent_support_categories")
    if blocking_conflict:
        blockers.append("conflicting_evidence")
    if lifecycle_flags_tuple:
        blockers.append("lifecycle_flag")
    if rejected_evidence_tuple:
        blockers.append("unverified_model_evidence")

    return EvidenceVerification(
        proposed_status=REVIEW_REQUIRED,
        proposed_score=min(
            0.79,
            0.15
            + (0.30 if target_identity_exact else 0.0)
            + 0.12 * len(support_categories),
        ),
        page_type=evidence.page_type,
        identity_result=identity_result,
        identity_evidence=identity_evidence_tuple,
        supporting_evidence=supporting_evidence_tuple,
        conflicting_evidence=conflicting_evidence_tuple,
        lifecycle_flags=lifecycle_flags_tuple,
        rejected_evidence=rejected_evidence_tuple,
        verification_result="INSUFFICIENT",
        reason="Automatic decision blocked: " + ", ".join(blockers),
    )
