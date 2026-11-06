from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


PAGE_TYPES = (
    "DETAIL_SINGLE",
    "LISTING_MULTI",
    "PORTAL",
    "ERROR_OR_EMPTY",
    "UNKNOWN",
)
SUPPORT_CATEGORIES = (
    "ORGANIZATION",
    "REGION",
    "APPLICATION_PERIOD",
    "DISTINCTIVE_TERM",
)
CONFLICT_CATEGORIES = (
    "POLICY_NUMBER",
    "POLICY_NAME",
    "ORGANIZATION",
    "REGION",
)
LIFECYCLE_CATEGORIES = (
    "CLOSED",
    "ENDED",
    "SUPERSEDED",
    "PAST_RECRUITMENT",
)

_ROOT_KEYS = {
    "page_type",
    "policy_identity",
    "supporting_evidence",
    "conflicts",
    "lifecycle_flags",
    "other_policy_identities",
}
_IDENTITY_KEYS = {"policy_numbers", "policy_names"}
_QUOTED_VALUE_KEYS = {"value", "quote"}
_CATEGORIZED_KEYS = {"category", "value", "quote"}
_LIFECYCLE_KEYS = {"category", "quote"}
_MAX_ITEMS = 20
_MAX_VALUE_CHARS = 300
_MAX_QUOTE_CHARS = 1000


class EvidenceSchemaError(ValueError):
    """Raised when an AI response does not match the closed evidence schema."""


@dataclass(frozen=True)
class QuotedValue:
    value: str
    quote: str


@dataclass(frozen=True)
class CategorizedEvidence:
    category: str
    value: str
    quote: str


@dataclass(frozen=True)
class LifecycleEvidence:
    category: str
    quote: str


@dataclass(frozen=True)
class PolicyIdentityEvidence:
    policy_numbers: tuple[QuotedValue, ...] = field(default_factory=tuple)
    policy_names: tuple[QuotedValue, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AiEvidence:
    page_type: str
    policy_identity: PolicyIdentityEvidence
    supporting_evidence: tuple[CategorizedEvidence, ...] = field(
        default_factory=tuple
    )
    conflicts: tuple[CategorizedEvidence, ...] = field(default_factory=tuple)
    lifecycle_flags: tuple[LifecycleEvidence, ...] = field(default_factory=tuple)
    other_policy_identities: tuple[QuotedValue, ...] = field(
        default_factory=tuple
    )


def _closed_object_schema(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


_QUOTED_VALUE_SCHEMA = _closed_object_schema(
    {
        "value": {"type": "string", "minLength": 1, "maxLength": _MAX_VALUE_CHARS},
        "quote": {"type": "string", "minLength": 2, "maxLength": _MAX_QUOTE_CHARS},
    }
)
_SUPPORT_SCHEMA = _closed_object_schema(
    {
        "category": {"type": "string", "enum": list(SUPPORT_CATEGORIES)},
        "value": {"type": "string", "minLength": 1, "maxLength": _MAX_VALUE_CHARS},
        "quote": {"type": "string", "minLength": 2, "maxLength": _MAX_QUOTE_CHARS},
    }
)
_CONFLICT_SCHEMA = _closed_object_schema(
    {
        "category": {"type": "string", "enum": list(CONFLICT_CATEGORIES)},
        "value": {"type": "string", "minLength": 1, "maxLength": _MAX_VALUE_CHARS},
        "quote": {"type": "string", "minLength": 2, "maxLength": _MAX_QUOTE_CHARS},
    }
)
_LIFECYCLE_SCHEMA = _closed_object_schema(
    {
        "category": {"type": "string", "enum": list(LIFECYCLE_CATEGORIES)},
        "quote": {"type": "string", "minLength": 2, "maxLength": _MAX_QUOTE_CHARS},
    }
)

# This is passed to an OpenAI-compatible structured-output request. The same
# contract is enforced again by parse_ai_evidence because gateway/model output
# is untrusted even when the provider claims JSON-schema conformance.
AI_EVIDENCE_JSON_SCHEMA: dict[str, Any] = _closed_object_schema(
    {
        "page_type": {"type": "string", "enum": list(PAGE_TYPES)},
        "policy_identity": _closed_object_schema(
            {
                "policy_numbers": {
                    "type": "array",
                    "items": _QUOTED_VALUE_SCHEMA,
                    "maxItems": _MAX_ITEMS,
                },
                "policy_names": {
                    "type": "array",
                    "items": _QUOTED_VALUE_SCHEMA,
                    "maxItems": _MAX_ITEMS,
                },
            }
        ),
        "supporting_evidence": {
            "type": "array",
            "items": _SUPPORT_SCHEMA,
            "maxItems": _MAX_ITEMS,
        },
        "conflicts": {
            "type": "array",
            "items": _CONFLICT_SCHEMA,
            "maxItems": _MAX_ITEMS,
        },
        "lifecycle_flags": {
            "type": "array",
            "items": _LIFECYCLE_SCHEMA,
            "maxItems": _MAX_ITEMS,
        },
        "other_policy_identities": {
            "type": "array",
            "items": _QUOTED_VALUE_SCHEMA,
            "maxItems": _MAX_ITEMS,
        },
    }
)


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceSchemaError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceSchemaError(f"{path} must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], path: str
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing={','.join(missing)}")
    if extra:
        details.append(f"extra={','.join(extra)}")
    raise EvidenceSchemaError(f"{path} has invalid keys ({'; '.join(details)})")


def _require_string(value: Any, path: str, *, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise EvidenceSchemaError(f"{path} must be a string")
    cleaned = value.strip()
    if len(cleaned) < minimum or len(cleaned) > maximum:
        raise EvidenceSchemaError(
            f"{path} must contain {minimum} to {maximum} characters"
        )
    return cleaned


def _require_array(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvidenceSchemaError(f"{path} must be an array")
    if len(value) > _MAX_ITEMS:
        raise EvidenceSchemaError(f"{path} must contain at most {_MAX_ITEMS} items")
    return value


def _parse_quoted_values(value: Any, path: str) -> tuple[QuotedValue, ...]:
    parsed: list[QuotedValue] = []
    for index, item in enumerate(_require_array(value, path)):
        item_path = f"{path}[{index}]"
        mapping = _require_mapping(item, item_path)
        _require_exact_keys(mapping, _QUOTED_VALUE_KEYS, item_path)
        parsed.append(
            QuotedValue(
                value=_require_string(
                    mapping["value"],
                    f"{item_path}.value",
                    maximum=_MAX_VALUE_CHARS,
                ),
                quote=_require_string(
                    mapping["quote"],
                    f"{item_path}.quote",
                    minimum=2,
                    maximum=_MAX_QUOTE_CHARS,
                ),
            )
        )
    return tuple(parsed)


def _parse_categorized(
    value: Any,
    path: str,
    allowed_categories: Sequence[str],
) -> tuple[CategorizedEvidence, ...]:
    parsed: list[CategorizedEvidence] = []
    for index, item in enumerate(_require_array(value, path)):
        item_path = f"{path}[{index}]"
        mapping = _require_mapping(item, item_path)
        _require_exact_keys(mapping, _CATEGORIZED_KEYS, item_path)
        category = _require_string(
            mapping["category"], f"{item_path}.category", maximum=50
        )
        if category not in allowed_categories:
            raise EvidenceSchemaError(
                f"{item_path}.category is not an allowed category"
            )
        parsed.append(
            CategorizedEvidence(
                category=category,
                value=_require_string(
                    mapping["value"],
                    f"{item_path}.value",
                    maximum=_MAX_VALUE_CHARS,
                ),
                quote=_require_string(
                    mapping["quote"],
                    f"{item_path}.quote",
                    minimum=2,
                    maximum=_MAX_QUOTE_CHARS,
                ),
            )
        )
    return tuple(parsed)


def _parse_lifecycle(value: Any, path: str) -> tuple[LifecycleEvidence, ...]:
    parsed: list[LifecycleEvidence] = []
    for index, item in enumerate(_require_array(value, path)):
        item_path = f"{path}[{index}]"
        mapping = _require_mapping(item, item_path)
        _require_exact_keys(mapping, _LIFECYCLE_KEYS, item_path)
        category = _require_string(
            mapping["category"], f"{item_path}.category", maximum=50
        )
        if category not in LIFECYCLE_CATEGORIES:
            raise EvidenceSchemaError(
                f"{item_path}.category is not an allowed category"
            )
        parsed.append(
            LifecycleEvidence(
                category=category,
                quote=_require_string(
                    mapping["quote"],
                    f"{item_path}.quote",
                    minimum=2,
                    maximum=_MAX_QUOTE_CHARS,
                ),
            )
        )
    return tuple(parsed)


def parse_ai_evidence(payload: str | Mapping[str, Any]) -> AiEvidence:
    """Parse an untrusted model response using a closed, bounded schema."""
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        except EvidenceSchemaError:
            raise
        except (json.JSONDecodeError, TypeError) as exc:
            raise EvidenceSchemaError("AI response is not valid JSON") from exc
    else:
        decoded = payload

    root = _require_mapping(decoded, "$.")
    _require_exact_keys(root, _ROOT_KEYS, "$." )
    page_type = _require_string(root["page_type"], "$.page_type", maximum=30)
    if page_type not in PAGE_TYPES:
        raise EvidenceSchemaError("$.page_type is not an allowed page type")

    identity = _require_mapping(root["policy_identity"], "$.policy_identity")
    _require_exact_keys(identity, _IDENTITY_KEYS, "$.policy_identity")
    return AiEvidence(
        page_type=page_type,
        policy_identity=PolicyIdentityEvidence(
            policy_numbers=_parse_quoted_values(
                identity["policy_numbers"],
                "$.policy_identity.policy_numbers",
            ),
            policy_names=_parse_quoted_values(
                identity["policy_names"],
                "$.policy_identity.policy_names",
            ),
        ),
        supporting_evidence=_parse_categorized(
            root["supporting_evidence"],
            "$.supporting_evidence",
            SUPPORT_CATEGORIES,
        ),
        conflicts=_parse_categorized(
            root["conflicts"],
            "$.conflicts",
            CONFLICT_CATEGORIES,
        ),
        lifecycle_flags=_parse_lifecycle(
            root["lifecycle_flags"], "$.lifecycle_flags"
        ),
        other_policy_identities=_parse_quoted_values(
            root["other_policy_identities"], "$.other_policy_identities"
        ),
    )
