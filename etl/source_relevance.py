from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit


VALIDATOR_VERSION = "policy-source-v2"

VERIFIED = "VERIFIED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
REJECTED = "REJECTED"
LINK_ONLY = "LINK_ONLY"

_ERROR_MARKERS = (
    "404",
    "notfound",
    "페이지를찾을수없",
    "존재하지않는페이지",
    "삭제된게시물",
    "접근할수없",
)
_BODY_ERROR_MARKERS = (
    "404error",
    "404notfound",
    "요청하신페이지를찾을수없",
    "페이지를찾을수없",
    "존재하지않는페이지",
    "존재하지않거나삭제",
    "삭제된게시물",
    "접근할수없",
)
_GENERIC_MARKERS = (
    "로그인",
    "통합검색",
    "검색결과",
    "신청시스템",
    "온라인신청",
    "청년포털",
    "홈페이지",
    "메인페이지",
)
_GENERIC_PATH_SEGMENTS = {
    "login",
    "signin",
    "search",
    "main",
    "index",
    "apply",
    "application",
}


def normalize_for_match(value: Any) -> str:
    """Normalize Korean and ASCII text for conservative exact containment."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _clean_values(values: Sequence[Any] | None) -> tuple[str, ...]:
    if not values:
        return ()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip() if value is not None else ""
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return tuple(result)


@dataclass(frozen=True)
class PolicyEvidence:
    policy_no: str
    policy_name: str
    organization_names: tuple[str, ...] = field(default_factory=tuple)
    region_names: tuple[str, ...] = field(default_factory=tuple)
    application_periods: tuple[str, ...] = field(default_factory=tuple)
    distinctive_terms: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, policy: Mapping[str, Any]) -> "PolicyEvidence":
        organizations = policy.get("organization_names")
        if organizations is None:
            organizations = (
                policy.get("supervising_org_name"),
                policy.get("operating_org_name"),
                policy.get("registrar_org_name"),
            )

        regions = policy.get("region_names") or ()
        periods = policy.get("application_periods") or ()
        distinctive_terms = policy.get("distinctive_terms") or ()
        return cls(
            policy_no=str(policy.get("policy_no") or "").strip(),
            policy_name=str(policy.get("policy_name") or "").strip(),
            organization_names=_clean_values(organizations),
            region_names=_clean_values(regions),
            application_periods=_clean_values(periods),
            distinctive_terms=_clean_values(distinctive_terms),
        )


@dataclass(frozen=True)
class PageEvidence:
    final_url: str
    title: str = ""
    h1: str = ""
    headings: tuple[str, ...] = field(default_factory=tuple)
    text: str = ""
    http_status: int | None = None
    declared_policy_no: str | None = None
    declared_policy_name: str | None = None
    organization_names: tuple[str, ...] = field(default_factory=tuple)
    region_names: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, page: Mapping[str, Any]) -> "PageEvidence":
        return cls(
            final_url=str(page.get("final_url") or "").strip(),
            title=str(page.get("title") or "").strip(),
            h1=str(page.get("h1") or "").strip(),
            headings=_clean_values(page.get("headings") or ()),
            text=str(page.get("text") or "").strip(),
            http_status=page.get("http_status"),
            declared_policy_no=(
                str(page["declared_policy_no"]).strip()
                if page.get("declared_policy_no") is not None
                else None
            ),
            declared_policy_name=(
                str(page["declared_policy_name"]).strip()
                if page.get("declared_policy_name") is not None
                else None
            ),
            organization_names=_clean_values(page.get("organization_names") or ()),
            region_names=_clean_values(page.get("region_names") or ()),
        )


@dataclass(frozen=True)
class ValidationResult:
    status: str
    score: float
    matched_signals: tuple[str, ...]
    conflicting_signals: tuple[str, ...]
    reason: str
    validator_version: str = VALIDATOR_VERSION


def _identifier_occurs(identifier: str, values: Sequence[str]) -> bool:
    identifier = identifier.strip()
    if not identifier:
        return False
    pattern = re.compile(
        rf"(?<![0-9A-Za-z]){re.escape(identifier)}(?![0-9A-Za-z])",
        flags=re.IGNORECASE,
    )
    return any(pattern.search(unquote(value or "")) for value in values)


def _contains_normalized(needle: str, haystack: str, *, min_length: int = 2) -> bool:
    normalized_needle = normalize_for_match(needle)
    return (
        len(normalized_needle) >= min_length
        and normalized_needle in normalize_for_match(haystack)
    )


def _equals_normalized(expected: str, observed: str, *, min_length: int = 4) -> bool:
    normalized_expected = normalize_for_match(expected)
    normalized_observed = normalize_for_match(observed)
    return (
        len(normalized_expected) >= min_length
        and normalized_expected == normalized_observed
    )


def _any_policy_value_in_page(values: Sequence[str], page_text: str) -> bool:
    return any(
        _contains_normalized(value, page_text, min_length=2)
        for value in values
    )


def _sets_conflict(expected: Sequence[str], observed: Sequence[str]) -> bool:
    if not expected or not observed:
        return False
    expected_norm = {normalize_for_match(value) for value in expected}
    observed_norm = {normalize_for_match(value) for value in observed}
    expected_norm.discard("")
    observed_norm.discard("")
    if not expected_norm or not observed_norm:
        return False
    return not any(
        expected_value in observed_value or observed_value in expected_value
        for expected_value in expected_norm
        for observed_value in observed_norm
    )


def _normalized_values(values: Sequence[str]) -> set[str]:
    return {
        normalized
        for value in values
        if (normalized := normalize_for_match(value))
    }


def _is_error_page(page: PageEvidence, combined_heading: str) -> bool:
    if page.http_status is not None and page.http_status >= 400:
        return True
    normalized_heading = normalize_for_match(combined_heading)
    if any(marker in normalized_heading for marker in _ERROR_MARKERS):
        return True

    # Some sites return a branded HTTP 200 page whose error message appears
    # only in the body. Limit the scan to the beginning to avoid treating a
    # legitimate policy document that merely mentions an error as a soft 404.
    normalized_body = normalize_for_match(page.text[:5000])
    return any(marker in normalized_body for marker in _BODY_ERROR_MARKERS)


def _is_generic_page(
    page: PageEvidence,
    *,
    policy_name_in_heading: bool,
    policy_identifier_found: bool,
) -> bool:
    try:
        parsed = urlsplit(page.final_url)
        path_parts = {
            part.casefold()
            for part in parsed.path.split("/")
            if part.strip()
        }
        root_without_query = parsed.path in ("", "/") and not parsed.query
    except ValueError:
        path_parts = set()
        root_without_query = False

    normalized_heading = normalize_for_match(" ".join((page.title, page.h1)))
    generic_heading = any(marker in normalized_heading for marker in _GENERIC_MARKERS)
    generic_path = bool(path_parts & _GENERIC_PATH_SEGMENTS)

    # A policy identifier or exact policy heading can make a generic-looking
    # site template specific. Login/search endpoints remain link metadata even
    # when an identifier is carried in the query string; an application page
    # must at least expose the exact policy heading to escape LINK_ONLY.
    hard_generic_path = bool(path_parts & {"login", "signin", "search"})
    if hard_generic_path:
        return True
    if generic_path and not policy_name_in_heading:
        return True
    if root_without_query and not (policy_identifier_found or policy_name_in_heading):
        return True
    return generic_heading and not policy_name_in_heading


def validate_policy_source(
    policy: PolicyEvidence | Mapping[str, Any],
    page: PageEvidence | Mapping[str, Any],
) -> ValidationResult:
    """Return a high-precision deterministic policy/page relevance decision.

    Embedding or LLM scores are deliberately absent. They may prioritize manual
    review, but cannot bypass the deterministic acceptance rules implemented
    here.
    """
    if not isinstance(policy, PolicyEvidence):
        policy = PolicyEvidence.from_mapping(policy)
    if not isinstance(page, PageEvidence):
        page = PageEvidence.from_mapping(page)

    if not policy.policy_no or not policy.policy_name:
        return ValidationResult(
            status=REVIEW_REQUIRED,
            score=0.0,
            matched_signals=(),
            conflicting_signals=("missing_policy_identity",),
            reason="The policy number or name is missing; automatic validation is unsafe.",
        )

    matched: list[str] = []
    conflicts: list[str] = []
    combined_heading = " ".join((page.title, page.h1, *page.headings))
    combined_page = "\n".join((combined_heading, page.text))

    if _is_error_page(page, combined_heading):
        return ValidationResult(
            status=REJECTED,
            score=0.0,
            matched_signals=(),
            conflicting_signals=("error_or_missing_page",),
            reason="The source is an HTTP error or missing-page response.",
        )

    declared_no = (page.declared_policy_no or "").strip()
    if declared_no:
        if declared_no.casefold() == policy.policy_no.casefold():
            matched.append("declared_policy_no_exact")
        else:
            conflicts.append("declared_policy_no_mismatch")

    identifier_in_url = _identifier_occurs(policy.policy_no, (page.final_url,))
    identifier_in_heading = _identifier_occurs(
        policy.policy_no,
        (page.title, page.h1, *page.headings),
    )
    identifier_in_body = _identifier_occurs(policy.policy_no, (page.text,))
    identifier_found = (
        identifier_in_url or identifier_in_heading or identifier_in_body
    )
    if identifier_found:
        matched.append("policy_no_exact")
    if identifier_in_url:
        matched.append("policy_no_url_exact")
    elif identifier_in_heading:
        matched.append("policy_no_heading_exact")
    elif identifier_in_body:
        matched.append("policy_no_body_exact")

    policy_name_in_heading = any(
        _equals_normalized(policy.policy_name, value, min_length=4)
        for value in (page.title, page.h1, *page.headings)
    )
    if policy_name_in_heading:
        matched.append("policy_name_heading_exact")
    elif _contains_normalized(policy.policy_name, page.text, min_length=4):
        matched.append("policy_name_body")

    declared_name = (page.declared_policy_name or "").strip()
    if declared_name:
        target_name = normalize_for_match(policy.policy_name)
        observed_name = normalize_for_match(declared_name)
        if target_name and target_name == observed_name:
            matched.append("declared_policy_name_match")
        else:
            conflicts.append("declared_policy_name_mismatch")

    if _sets_conflict(policy.organization_names, page.organization_names):
        conflicts.append("organization_conflict")
    elif _any_policy_value_in_page(policy.organization_names, combined_page):
        matched.append("organization_match")

    if _sets_conflict(policy.region_names, page.region_names):
        conflicts.append("region_conflict")
    elif _any_policy_value_in_page(policy.region_names, combined_page):
        matched.append("region_match")

    if _any_policy_value_in_page(policy.application_periods, combined_page):
        matched.append("application_period_match")

    if _any_policy_value_in_page(policy.distinctive_terms, combined_page):
        matched.append("distinctive_information_match")

    matched = list(dict.fromkeys(matched))
    conflicts = list(dict.fromkeys(conflicts))

    if conflicts:
        return ValidationResult(
            status=REJECTED,
            score=0.0,
            matched_signals=tuple(matched),
            conflicting_signals=tuple(conflicts),
            reason="A strong policy identity, organization, or region conflict was found.",
        )

    if _is_generic_page(
        page,
        policy_name_in_heading=policy_name_in_heading,
        policy_identifier_found=identifier_found,
    ):
        return ValidationResult(
            status=LINK_ONLY,
            score=0.2 if matched else 0.05,
            matched_signals=tuple(matched),
            conflicting_signals=(),
            reason="The URL is a generic portal, login, search, or application endpoint; do not extract policy facts.",
        )

    # A policy number buried in body text may come from a listing page that
    # mentions many policies. Only URL/explicit metadata is strong enough to
    # approve without the exact heading + independent-signal rule.
    identifier_exact = (
        identifier_in_url or "declared_policy_no_exact" in matched
    )
    supporting_categories = {
        signal
        for signal in matched
        if signal
        in {
            "organization_match",
            "region_match",
            "application_period_match",
            "distinctive_information_match",
        }
    }
    # A metropolitan government name can simultaneously be an organization
    # and a region (for example, "인천광역시"). Do not count the same literal
    # evidence twice toward the two-independent-signal approval threshold.
    if {
        "organization_match",
        "region_match",
    }.issubset(supporting_categories) and (
        _normalized_values(policy.organization_names)
        & _normalized_values(policy.region_names)
    ):
        supporting_categories.remove("region_match")

    # A policy number in a URL can be an untrusted query parameter copied onto
    # a generic or unrelated page. It is strong identity evidence only when
    # the page itself also identifies the policy. Never approve on URL ID
    # alone.
    if identifier_exact and policy_name_in_heading:
        return ValidationResult(
            status=VERIFIED,
            score=0.98,
            matched_signals=tuple(matched),
            conflicting_signals=(),
            reason="The exact policy number and exact policy heading both match.",
        )

    if (
        identifier_exact
        and "policy_name_body" in matched
        and supporting_categories
    ):
        return ValidationResult(
            status=VERIFIED,
            score=0.95,
            matched_signals=tuple(matched),
            conflicting_signals=(),
            reason=(
                "The exact policy number, policy name, and an independent "
                "supporting signal match."
            ),
        )

    if policy_name_in_heading and len(supporting_categories) >= 2:
        score = min(0.99, 0.65 + 0.15 * len(supporting_categories))
        return ValidationResult(
            status=VERIFIED,
            score=score,
            matched_signals=tuple(matched),
            conflicting_signals=(),
            reason="The exact policy heading and at least two independent supporting signals match.",
        )

    score = 0.15
    if policy_name_in_heading:
        score += 0.35
    if "policy_name_body" in matched:
        score += 0.15
    score += 0.1 * len(supporting_categories)
    return ValidationResult(
        status=REVIEW_REQUIRED,
        score=min(score, 0.79),
        matched_signals=tuple(matched),
        conflicting_signals=(),
        reason="The page has insufficient independent evidence for automatic approval.",
    )
