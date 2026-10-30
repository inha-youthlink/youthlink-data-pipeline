from __future__ import annotations

import argparse
import html
import ipaddress
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)

RELATION_FIELDS = (
    ("APPLICATION", "application_url"),
    ("REFERENCE_1", "reference_url_1"),
    ("REFERENCE_2", "reference_url_2"),
)

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


class UrlNormalizationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NormalizedUrl:
    canonical_url: str
    scheme: str
    host: str


@dataclass(frozen=True)
class PolicySourceLink:
    policy_no: str
    relation_type: str
    original_url: str
    canonical_url: str
    scheme: str
    host: str


@dataclass(frozen=True)
class DiscoveryIssue:
    policy_no: str
    relation_type: str
    code: str
    reason: str


@dataclass(frozen=True)
class DiscoveryResult:
    active_policy_count: int
    links: tuple[PolicySourceLink, ...]
    issues: tuple[DiscoveryIssue, ...]

    @property
    def unique_source_count(self) -> int:
        return len({link.canonical_url for link in self.links})


def _validate_host(host: str) -> str:
    lowered = host.rstrip(".").lower()
    if not lowered:
        raise UrlNormalizationError("MISSING_HOST", "URL host is missing")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise UrlNormalizationError("BLOCKED_HOST", "localhost is not allowed")

    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        try:
            ascii_host = lowered.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise UrlNormalizationError(
                "INVALID_HOST", "URL host is not valid IDNA"
            ) from error

        if "." not in ascii_host:
            raise UrlNormalizationError(
                "NON_PUBLIC_HOST", "URL host must be a public domain"
            )
        if any(not _HOST_LABEL.fullmatch(label) for label in ascii_host.split(".")):
            raise UrlNormalizationError(
                "INVALID_HOST", "URL host contains an invalid label"
            )
        return ascii_host

    # Fetch-time validation must additionally resolve DNS immediately before
    # every request. Discovery can only reject literal non-global addresses.
    if not address.is_global:
        raise UrlNormalizationError(
            "BLOCKED_ADDRESS", "non-public IP addresses are not allowed"
        )
    return address.compressed.lower()


def normalize_url(value: Any) -> NormalizedUrl:
    if value is None:
        raise UrlNormalizationError("EMPTY", "URL is empty")

    original = html.unescape(str(value)).strip()
    if not original:
        raise UrlNormalizationError("EMPTY", "URL is empty")
    if len(original) > 4096:
        raise UrlNormalizationError("TOO_LONG", "URL exceeds 4096 characters")
    if _CONTROL_CHARACTER.search(original):
        raise UrlNormalizationError(
            "CONTROL_CHARACTER", "URL contains a control character"
        )

    candidate = original
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as error:
        raise UrlNormalizationError("INVALID_URL", "URL cannot be parsed") from error

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UrlNormalizationError(
            "UNSUPPORTED_SCHEME", "only http and https URLs are allowed"
        )
    if parts.username is not None or parts.password is not None:
        raise UrlNormalizationError(
            "EMBEDDED_CREDENTIALS", "credentials in URLs are not allowed"
        )

    host = _validate_host(parts.hostname or "")
    if port is not None and not (1 <= port <= 65535):
        raise UrlNormalizationError("INVALID_PORT", "URL port is out of range")

    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    bracketed_host = f"[{host}]" if ":" in host else host
    netloc = bracketed_host
    if port is not None and not default_port:
        netloc += f":{port}"

    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&?/:;+,%@-._~!$'()*")
    canonical = urlunsplit((scheme, netloc, path, query, ""))
    return NormalizedUrl(canonical_url=canonical, scheme=scheme, host=host)


def discover_policy_sources(
    rows: Iterable[Mapping[str, Any]],
) -> DiscoveryResult:
    links: dict[tuple[str, str, str], PolicySourceLink] = {}
    issues: list[DiscoveryIssue] = []
    policies: set[str] = set()

    for row in rows:
        policy_no = str(row["policy_no"])
        policies.add(policy_no)
        for relation_type, field_name in RELATION_FIELDS:
            raw = row.get(field_name)
            if raw is None or not str(raw).strip():
                continue
            try:
                normalized = normalize_url(raw)
            except UrlNormalizationError as error:
                issues.append(
                    DiscoveryIssue(
                        policy_no=policy_no,
                        relation_type=relation_type,
                        code=error.code,
                        reason=str(error),
                    )
                )
                continue

            link = PolicySourceLink(
                policy_no=policy_no,
                relation_type=relation_type,
                original_url=str(raw).strip(),
                canonical_url=normalized.canonical_url,
                scheme=normalized.scheme,
                host=normalized.host,
            )
            links[(policy_no, relation_type, normalized.canonical_url)] = link

    return DiscoveryResult(
        active_policy_count=len(policies),
        links=tuple(
            sorted(
                links.values(),
                key=lambda item: (
                    item.policy_no,
                    item.relation_type,
                    item.canonical_url,
                ),
            )
        ),
        issues=tuple(
            sorted(
                issues,
                key=lambda item: (item.policy_no, item.relation_type, item.code),
            )
        ),
    )


def load_active_policy_urls(
    conn: psycopg.Connection[Any],
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT policy_no, application_url, reference_url_1, reference_url_2
            FROM policy
            WHERE is_active = TRUE
            ORDER BY policy_no
            """
        )
        return list(cur.fetchall())


def sync_policy_sources(
    conn: psycopg.Connection[Any],
    result: DiscoveryResult,
) -> dict[str, int]:
    """Persist valid links without changing any relevance verdict."""
    source_ids: dict[str, Any] = {}
    unique_sources = {
        link.canonical_url: (link.scheme, link.host)
        for link in result.links
    }

    with conn.cursor() as cur:
        for canonical_url, (scheme, host) in sorted(unique_sources.items()):
            cur.execute(
                """
                INSERT INTO web_source (canonical_url, scheme, host)
                VALUES (%s, %s, %s)
                ON CONFLICT (canonical_url) DO UPDATE
                SET scheme = EXCLUDED.scheme,
                    host = EXCLUDED.host
                RETURNING source_id
                """,
                (canonical_url, scheme, host),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("web_source UPSERT did not return source_id")
            source_ids[canonical_url] = row[0]

        cur.execute(
            """
            CREATE TEMP TABLE current_policy_web_source_link (
                policy_no VARCHAR(30) NOT NULL,
                source_id UUID NOT NULL,
                relation_type VARCHAR(30) NOT NULL,
                PRIMARY KEY (policy_no, source_id, relation_type)
            ) ON COMMIT DROP
            """
        )

        for link in result.links:
            source_id = source_ids[link.canonical_url]
            cur.execute(
                """
                INSERT INTO policy_web_source (
                    policy_no,
                    source_id,
                    relation_type,
                    original_url,
                    is_active,
                    last_seen_at,
                    deactivated_at
                )
                VALUES (%s, %s, %s, %s, TRUE, NOW(), NULL)
                ON CONFLICT (policy_no, source_id, relation_type) DO UPDATE
                SET original_url = EXCLUDED.original_url,
                    is_active = TRUE,
                    last_seen_at = NOW(),
                    deactivated_at = NULL,
                    validation_status = CASE
                        WHEN policy_web_source.is_active = FALSE THEN 'PENDING'
                        ELSE policy_web_source.validation_status
                    END,
                    validation_score = CASE
                        WHEN policy_web_source.is_active = FALSE THEN NULL
                        ELSE policy_web_source.validation_score
                    END,
                    matched_signals = CASE
                        WHEN policy_web_source.is_active = FALSE THEN '[]'::jsonb
                        ELSE policy_web_source.matched_signals
                    END,
                    conflicting_signals = CASE
                        WHEN policy_web_source.is_active = FALSE THEN '[]'::jsonb
                        ELSE policy_web_source.conflicting_signals
                    END,
                    validation_reason = CASE
                        WHEN policy_web_source.is_active = FALSE THEN NULL
                        ELSE policy_web_source.validation_reason
                    END,
                    validator_version = CASE
                        WHEN policy_web_source.is_active = FALSE THEN NULL
                        ELSE policy_web_source.validator_version
                    END,
                    validated_at = CASE
                        WHEN policy_web_source.is_active = FALSE THEN NULL
                        ELSE policy_web_source.validated_at
                    END,
                    validated_document_id = CASE
                        WHEN policy_web_source.is_active = FALSE THEN NULL
                        ELSE policy_web_source.validated_document_id
                    END,
                    reviewed_by = CASE
                        WHEN policy_web_source.is_active = FALSE THEN NULL
                        ELSE policy_web_source.reviewed_by
                    END,
                    reviewed_at = CASE
                        WHEN policy_web_source.is_active = FALSE THEN NULL
                        ELSE policy_web_source.reviewed_at
                    END
                """,
                (
                    link.policy_no,
                    source_id,
                    link.relation_type,
                    link.original_url,
                ),
            )
            cur.execute(
                """
                INSERT INTO current_policy_web_source_link (
                    policy_no, source_id, relation_type
                )
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (link.policy_no, source_id, link.relation_type),
            )

        cur.execute(
            """
            UPDATE policy_web_source AS link
            SET is_active = FALSE,
                deactivated_at = COALESCE(link.deactivated_at, NOW())
            WHERE link.is_active = TRUE
              AND (
                  NOT EXISTS (
                      SELECT 1
                      FROM policy AS p
                      WHERE p.policy_no = link.policy_no
                        AND p.is_active = TRUE
                  )
                  OR NOT EXISTS (
                      SELECT 1
                      FROM current_policy_web_source_link AS current_link
                      WHERE current_link.policy_no = link.policy_no
                        AND current_link.source_id = link.source_id
                        AND current_link.relation_type = link.relation_type
                  )
              )
            """
        )
        deactivated = cur.rowcount

    return {
        "sources": len(unique_sources),
        "links": len(result.links),
        "issues": len(result.issues),
        "deactivated": deactivated,
    }


def print_report(
    result: DiscoveryResult,
    *,
    top_domains: int,
    show_issues: int,
) -> None:
    print("policy source discovery (no network requests)")
    print(f"active_policies={result.active_policy_count}")
    print(f"valid_links={len(result.links)}")
    print(f"unique_sources={result.unique_source_count}")
    print(f"issues={len(result.issues)}")

    domain_counts = Counter(link.host for link in result.links)
    if top_domains:
        print("\ntop domains")
        for host, count in domain_counts.most_common(top_domains):
            print(f"{count:5d}  {host}")

    issue_counts = Counter(issue.code for issue in result.issues)
    if issue_counts:
        print("\nissue types")
        for code, count in issue_counts.most_common():
            print(f"{count:5d}  {code}")

    if show_issues:
        print("\nissue samples (URL values are intentionally hidden)")
        for issue in result.issues[:show_issues]:
            print(
                f"{issue.policy_no} {issue.relation_type} "
                f"{issue.code}: {issue.reason}"
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover and normalize policy source URLs without fetching them."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write valid sources and links to PostgreSQL (default: dry-run)",
    )
    parser.add_argument(
        "--top-domains",
        type=int,
        default=20,
        help="number of domains to print (default: 20)",
    )
    parser.add_argument(
        "--show-issues",
        type=int,
        default=20,
        help="number of invalid-link summaries to print (default: 20)",
    )
    args = parser.parse_args(argv)
    if args.top_domains < 0:
        parser.error("--top-domains must not be negative")
    if args.show_issues < 0:
        parser.error("--show-issues must not be negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    with psycopg.connect(DB_DSN) as conn:
        result = discover_policy_sources(load_active_policy_urls(conn))
        print_report(
            result,
            top_domains=args.top_domains,
            show_issues=args.show_issues,
        )
        if not args.write:
            print(
                "\ndry-run only; add --write after migrations 009 and 010 "
                "to persist links"
            )
            return 0

        stats = sync_policy_sources(conn, result)
        conn.commit()

    print(
        "\nsynced policy sources: "
        f"sources={stats['sources']}, links={stats['links']}, "
        f"issues={stats['issues']}, deactivated={stats['deactivated']}"
    )
    print("all new links remain PENDING until relevance validation succeeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
