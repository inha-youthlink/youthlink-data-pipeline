from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from uuid import UUID

import psycopg
import requests
from dotenv import load_dotenv
from lxml import etree, html as lxml_html
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

try:
    from .source_relevance import (
        REVIEW_REQUIRED,
        VERIFIED,
        PageEvidence,
        PolicyEvidence,
        validate_policy_source,
    )
    from .sync_policy_sources import (
        NormalizedUrl,
        UrlNormalizationError,
        normalize_url,
    )
except ImportError:  # Direct execution: python etl/crawl_policy_sources.py
    from source_relevance import (
        REVIEW_REQUIRED,
        VERIFIED,
        PageEvidence,
        PolicyEvidence,
        validate_policy_source,
    )
    from sync_policy_sources import NormalizedUrl, UrlNormalizationError, normalize_url


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
DEFAULT_USER_AGENT = os.getenv(
    "YOUTHLINK_CRAWLER_USER_AGENT",
    "YouthLinkResearchBot/0.1",
).strip()
DEFAULT_REQUEST_DELAY_SECONDS = float(
    os.getenv("YOUTHLINK_CRAWLER_REQUEST_DELAY_SECONDS", "2")
)
DEFAULT_TIMEOUT_SECONDS = float(
    os.getenv("YOUTHLINK_CRAWLER_TIMEOUT_SECONDS", "15")
)
DEFAULT_MAX_RESPONSE_BYTES = int(
    os.getenv("YOUTHLINK_CRAWLER_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024))
)
MAX_REDIRECTS = 5
ROBOTS_MAX_BYTES = 256 * 1024
EXTRACTOR_NAME = "lxml-main-text"
EXTRACTOR_VERSION = "web-extractor-v3"
NAVER_BLOG_HOST = "blog.naver.com"
NAVER_POST_PATH = "/PostView.naver"
NAVER_IFRAME_MODE = "NAVER_POST_IFRAME"

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CONTENT_CLASS_MARKERS = (
    "article",
    "board",
    "content",
    "detail",
    "policy",
    "view",
)
_REMOVED_TAGS = (
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "form",
    "noscript",
    "svg",
    "iframe",
    "aside",
)


class FetchFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        fetch_status: str = "FAILED",
        diagnostics: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.fetch_status = fetch_status
        self.diagnostics = dict(diagnostics or {})
        # Fail immediately if a caller tries to attach response objects,
        # bytes, or any other value that cannot safely be persisted as JSON.
        json.dumps(self.diagnostics, ensure_ascii=False)

    def add_diagnostics(self, **values: Any) -> None:
        combined = {**self.diagnostics, **values}
        json.dumps(combined, ensure_ascii=False)
        self.diagnostics = combined


@dataclass(frozen=True)
class CrawlCandidate:
    source_id: Any
    canonical_url: str
    host: str
    etag: str | None
    http_last_modified: str | None
    priority_score: int


@dataclass(frozen=True)
class HttpPayload:
    final_url: str
    status_code: int
    content_type: str | None
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class ExtractedPage:
    title: str
    h1: str
    headings: tuple[str, ...]
    text: str
    content_hash: str
    retrieval: dict[str, Any] = field(default_factory=dict)


def validate_non_negative(value: float, name: str) -> float:
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def validate_positive(value: int | float, name: str) -> int | float:
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def resolve_public_addresses(target: NormalizedUrl) -> tuple[str, ...]:
    """Resolve immediately before a request and reject every non-global IP."""
    parsed = urlsplit(target.canonical_url)
    port = parsed.port or (443 if target.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(
            target.host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise FetchFailure(
            "DNS_ERROR",
            "The source host could not be resolved",
        ) from None

    addresses = sorted({answer[4][0] for answer in answers})
    if not addresses:
        raise FetchFailure("DNS_EMPTY", "The source host resolved to no addresses")

    for address_text in addresses:
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            raise FetchFailure(
                "DNS_INVALID_ADDRESS",
                "The source host returned an invalid address",
            ) from None
        if not address.is_global:
            raise FetchFailure(
                "BLOCKED_ADDRESS",
                "The source host resolves to a non-public address",
                fetch_status="BLOCKED",
            )
    return tuple(addresses)


def _read_bounded_response(
    response: requests.Response,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise FetchFailure(
                "RESPONSE_TOO_LARGE",
                f"The source response exceeded {max_bytes} bytes",
                status_code=response.status_code,
                fetch_status="UNSUPPORTED",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def request_with_redirects(
    session: requests.Session,
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float,
    max_bytes: int,
    extra_headers: dict[str, str] | None = None,
    request_guard: Callable[[NormalizedUrl], None] | None = None,
) -> HttpPayload:
    try:
        current = normalize_url(url).canonical_url
    except UrlNormalizationError as error:
        raise FetchFailure(
            "BLOCKED_URL",
            "The source URL failed the public URL safety check",
            fetch_status="BLOCKED",
        ) from error
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.2",
    }
    if extra_headers:
        headers.update(extra_headers)

    for _ in range(MAX_REDIRECTS + 1):
        try:
            target = normalize_url(current)
        except UrlNormalizationError as error:
            raise FetchFailure(
                "BLOCKED_REDIRECT",
                "The source redirected to a URL that failed the public URL safety check",
                fetch_status="BLOCKED",
            ) from error
        if request_guard is not None:
            request_guard(target)
        resolve_public_addresses(target)
        try:
            with session.get(
                target.canonical_url,
                headers=headers,
                timeout=timeout_seconds,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchFailure(
                            "REDIRECT_WITHOUT_LOCATION",
                            "The source returned a redirect without a location",
                            status_code=response.status_code,
                        )
                    current = urljoin(target.canonical_url, location)
                    continue

                body = _read_bounded_response(response, max_bytes)
                return HttpPayload(
                    final_url=target.canonical_url,
                    status_code=response.status_code,
                    content_type=response.headers.get("Content-Type"),
                    headers={
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    },
                    body=body,
                )
        except FetchFailure:
            raise
        except (requests.ConnectionError, requests.Timeout):
            raise FetchFailure(
                "NETWORK_ERROR",
                "The source request failed due to a network error",
            ) from None
        except requests.RequestException:
            # Requests errors can contain the full URL and sensitive query
            # values. Do not propagate their original text.
            raise FetchFailure(
                "REQUEST_ERROR",
                "The source request failed",
            ) from None

    raise FetchFailure(
        "TOO_MANY_REDIRECTS",
        f"The source exceeded {MAX_REDIRECTS} redirects",
        fetch_status="BLOCKED",
    )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class RobotsCache:
    def __init__(self) -> None:
        self._parsers: dict[str, RobotFileParser | None] = {}

    def can_fetch(
        self,
        session: requests.Session,
        url: str,
        *,
        user_agent: str,
        timeout_seconds: float,
    ) -> bool:
        origin = _origin(url)
        if origin not in self._parsers:
            robots_url = origin + "/robots.txt"
            try:
                response = request_with_redirects(
                    session,
                    robots_url,
                    user_agent=user_agent,
                    timeout_seconds=timeout_seconds,
                    max_bytes=ROBOTS_MAX_BYTES,
                    extra_headers={"Accept": "text/plain,*/*;q=0.1"},
                )
            except FetchFailure:
                # High-precision/safe default: if crawl policy cannot be
                # checked, do not fetch page content automatically.
                self._parsers[origin] = None
            else:
                if response.status_code == 404:
                    parser = RobotFileParser()
                    parser.parse([])
                    self._parsers[origin] = parser
                elif 200 <= response.status_code < 300:
                    parser = RobotFileParser()
                    text = decode_body(response.body, response.content_type)
                    parser.parse(text.splitlines())
                    self._parsers[origin] = parser
                else:
                    self._parsers[origin] = None

        parser = self._parsers[origin]
        return parser is not None and parser.can_fetch(user_agent, url)


def decode_body(body: bytes, content_type: str | None) -> str:
    charset = None
    if content_type:
        match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, re.I)
        if match:
            charset = match.group(1).strip()
    for encoding in (charset, "utf-8", "cp949", "euc-kr"):
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _normalized_text(element: etree._Element) -> str:
    raw = element.text_content()
    lines: list[str] = []
    previous = None
    for line in raw.splitlines():
        cleaned = re.sub(r"[ \t\f\v]+", " ", line).strip()
        if not cleaned or cleaned == previous:
            continue
        lines.append(cleaned)
        previous = cleaned
    return "\n".join(lines).strip()


def extract_page(html_text: str) -> ExtractedPage:
    try:
        document = lxml_html.fromstring(html_text)
    except (etree.ParserError, ValueError):
        raise FetchFailure(
            "INVALID_HTML",
            "The source returned HTML that could not be parsed",
            fetch_status="UNSUPPORTED",
            diagnostics={"html_chars": len(html_text)},
        ) from None

    title = " ".join(document.xpath("//title[1]//text()"))
    title = re.sub(r"\s+", " ", title).strip()
    script_count = len(document.xpath("//script"))
    iframe_count = len(document.xpath("//iframe"))

    etree.strip_elements(document, *_REMOVED_TAGS, with_tail=False)
    candidates: list[etree._Element] = []
    candidates.extend(document.xpath("//main | //article | //*[@role='main']"))
    for marker in _CONTENT_CLASS_MARKERS:
        candidates.extend(
            document.xpath(
                "//*[contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz'), $marker) or "
                "contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz'), $marker)]",
                marker=marker,
            )
        )
    best_text = ""
    best_element: etree._Element | None = None
    seen_elements: set[int] = set()
    for candidate in candidates:
        identity = id(candidate)
        if identity in seen_elements:
            continue
        seen_elements.add(identity)
        text = _normalized_text(candidate)
        if len(text) > len(best_text):
            best_text = text
            best_element = candidate

    candidate_text_chars = len(best_text)
    body_text_chars = 0

    # Prefer an article/main/content container. Falling back to the body is
    # only allowed when no semantic candidate has enough text; otherwise the
    # page's unrelated navigation and policy lists can pollute validation.
    if len(best_text) < 200:
        bodies = document.xpath("//body")
        best_element = bodies[0] if bodies else None
        best_text = _normalized_text(best_element) if best_element is not None else ""
        body_text_chars = len(best_text)

    if len(best_text) < 200:
        raise FetchFailure(
            "INSUFFICIENT_TEXT",
            "The extracted page text is too short for policy validation",
            fetch_status="UNSUPPORTED",
            diagnostics={
                "html_chars": len(html_text),
                "title_chars": len(title),
                "script_count": script_count,
                "iframe_count": iframe_count,
                "semantic_candidate_count": len(seen_elements),
                "candidate_text_chars": candidate_text_chars,
                "body_text_chars": body_text_chars,
                "selected_text_chars": len(best_text),
            },
        )

    # Only headings inside the selected content container are evidence. A
    # global site header or unrelated sidebar title must never make a policy
    # page look relevant.
    heading_root = best_element if best_element is not None else document
    h1_values = [
        re.sub(r"\s+", " ", value).strip()
        for value in heading_root.xpath(".//h1//text()")
        if str(value).strip()
    ]
    h1 = h1_values[0] if h1_values else ""
    headings = tuple(
        dict.fromkeys(
            re.sub(r"\s+", " ", value).strip()
            for value in heading_root.xpath(
                ".//h1//text() | .//h2//text() | .//h3//text()"
            )
            if str(value).strip()
        )
    )
    best_text = best_text[:200_000]
    hash_input = "\n".join((title, h1, *headings, best_text))
    return ExtractedPage(
        title=title,
        h1=h1,
        headings=headings,
        text=best_text,
        content_hash=hashlib.sha256(hash_input.encode("utf-8")).hexdigest(),
    )


def extract_naver_post_page(html_text: str) -> ExtractedPage:
    """Extract only the SmartEditor post body from an exact Naver post.

    The PostView document contains blog navigation, notices, neighboring-post
    lists, comments, and serialized page data outside the actual post. Those
    elements are not policy evidence and must never become RAG chunks.
    """
    try:
        document = lxml_html.fromstring(html_text)
    except (etree.ParserError, ValueError):
        raise FetchFailure(
            "INVALID_NAVER_POST_HTML",
            "The Naver post HTML could not be parsed",
            fetch_status="UNSUPPORTED",
            diagnostics={"html_chars": len(html_text)},
        ) from None

    containers = document.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' se-main-container ')]"
    )
    if len(containers) != 1:
        raise FetchFailure(
            "INVALID_NAVER_POST_CONTAINER",
            "The Naver post did not expose exactly one supported content container",
            fetch_status="UNSUPPORTED",
            diagnostics={
                "html_chars": len(html_text),
                "content_container_count": len(containers),
            },
        )

    title_values = [
        re.sub(r"\s+", " ", str(value)).strip()
        for value in document.xpath("//meta[@property='og:title'][1]/@content")
        if str(value).strip()
    ]
    if not title_values:
        title_values = [
            re.sub(r"\s+", " ", str(value)).strip()
            for value in document.xpath("//title[1]//text()")
            if str(value).strip()
        ]
    title = title_values[0] if title_values else ""
    content_root = containers[0]
    etree.strip_elements(content_root, *_REMOVED_TAGS, with_tail=False)
    text = _normalized_text(content_root)
    if len(text) < 200:
        raise FetchFailure(
            "INSUFFICIENT_NAVER_POST_TEXT",
            "The supported Naver post body is too short for policy validation",
            fetch_status="UNSUPPORTED",
            diagnostics={
                "html_chars": len(html_text),
                "title_chars": len(title),
                "content_container_count": len(containers),
                "selected_text_chars": len(text),
            },
        )

    h1_values = [
        re.sub(r"\s+", " ", str(value)).strip()
        for value in content_root.xpath(".//h1//text()")
        if str(value).strip()
    ]
    h1 = h1_values[0] if h1_values else ""
    headings = tuple(
        dict.fromkeys(
            re.sub(r"\s+", " ", str(value)).strip()
            for value in content_root.xpath(
                ".//h1//text() | .//h2//text() | .//h3//text()"
            )
            if str(value).strip()
        )
    )
    text = text[:200_000]
    hash_input = "\n".join((title, h1, *headings, text))
    return ExtractedPage(
        title=title,
        h1=h1,
        headings=headings,
        text=text,
        content_hash=hashlib.sha256(hash_input.encode("utf-8")).hexdigest(),
    )


def _response_diagnostics(response: HttpPayload) -> dict[str, Any]:
    """Return non-sensitive response metadata without body or query values."""
    parsed = urlsplit(response.final_url)
    return {
        "http_status": response.status_code,
        "content_type": (response.content_type or "")[:255],
        "response_bytes": len(response.body),
        "final_host": (parsed.hostname or "").lower()[:253],
        "final_path": (parsed.path or "/")[:1000],
    }


def _extract_html_response(
    response: HttpPayload,
    *,
    extractor: Callable[[str], ExtractedPage] = extract_page,
) -> ExtractedPage:
    if response.status_code in {401, 403}:
        raise FetchFailure(
            "ACCESS_BLOCKED",
            "The source denied automated access",
            status_code=response.status_code,
            fetch_status="BLOCKED",
            diagnostics=_response_diagnostics(response),
        )
    if response.status_code >= 400:
        raise FetchFailure(
            "HTTP_ERROR",
            f"The source returned HTTP {response.status_code}",
            status_code=response.status_code,
            diagnostics=_response_diagnostics(response),
        )

    media_type = (response.content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in {"text/html", "application/xhtml+xml"}:
        raise FetchFailure(
            "UNSUPPORTED_CONTENT_TYPE",
            "The source is not an HTML document",
            status_code=response.status_code,
            fetch_status="UNSUPPORTED",
            diagnostics=_response_diagnostics(response),
        )
    try:
        return extractor(decode_body(response.body, response.content_type))
    except FetchFailure as error:
        error.status_code = response.status_code
        error.add_diagnostics(**_response_diagnostics(response))
        raise


def _naver_post_identity(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or parsed.port is not None:
        return None
    if (parsed.hostname or "").lower() != NAVER_BLOG_HOST:
        return None
    segments = [unquote(value) for value in parsed.path.split("/") if value]
    if len(segments) != 2:
        return None
    blog_id, log_no = segments
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,100}", blog_id):
        return None
    if not re.fullmatch(r"[0-9]{1,30}", log_no):
        return None
    return blog_id, log_no


def _matches_naver_content_target(
    target_url: str,
    *,
    blog_id: str,
    log_no: str,
) -> bool:
    parsed = urlsplit(target_url)
    if parsed.scheme.lower() != "https" or parsed.port is not None:
        return False
    if (parsed.hostname or "").lower() != NAVER_BLOG_HOST:
        return False
    if parsed.path != NAVER_POST_PATH:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    return query.get("blogId") == [blog_id] and query.get("logNo") == [log_no]


def _naver_iframe_target(
    container_url: str,
    html_text: str,
) -> tuple[str, str, str] | None:
    identity = _naver_post_identity(container_url)
    if identity is None:
        return None
    blog_id, log_no = identity
    try:
        document = lxml_html.fromstring(html_text)
    except (etree.ParserError, ValueError):
        return None
    frames = document.xpath(
        "//iframe[(@id='mainFrame' or @name='mainFrame') and @src]"
    )
    if not frames:
        return None
    if len(frames) != 1:
        raise FetchFailure(
            "AMBIGUOUS_NAVER_IFRAME",
            "The Naver post exposed more than one main content frame",
            fetch_status="UNSUPPORTED",
            diagnostics={"matching_iframe_count": len(frames)},
        )

    raw_target = urljoin(container_url, str(frames[0].get("src") or ""))
    try:
        target = normalize_url(raw_target)
    except UrlNormalizationError as error:
        raise FetchFailure(
            "BLOCKED_NAVER_IFRAME_URL",
            "The Naver content frame failed the public URL safety check",
            fetch_status="BLOCKED",
        ) from error
    if not _matches_naver_content_target(
        target.canonical_url,
        blog_id=blog_id,
        log_no=log_no,
    ):
        raise FetchFailure(
            "BLOCKED_NAVER_IFRAME_TARGET",
            "The Naver content frame did not match the requested post",
            fetch_status="BLOCKED",
        )
    return target.canonical_url, blog_id, log_no


def fetch_html_page(
    session: requests.Session,
    robots: RobotsCache,
    candidate: CrawlCandidate,
    *,
    user_agent: str,
    timeout_seconds: float,
    max_bytes: int,
) -> tuple[HttpPayload, ExtractedPage | None]:
    requested_naver_identity = _naver_post_identity(candidate.canonical_url)
    if candidate.host == NAVER_BLOG_HOST and requested_naver_identity is None:
        raise FetchFailure(
            "UNSUPPORTED_NAVER_NON_POST_URL",
            "Only an exact Naver blog post URL can use the iframe adapter",
            fetch_status="UNSUPPORTED",
        )

    request_guard: Callable[[NormalizedUrl], None] | None = None
    if requested_naver_identity is None:
        if not robots.can_fetch(
            session,
            candidate.canonical_url,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
        ):
            raise FetchFailure(
                "ROBOTS_BLOCKED",
                "The source is disallowed or its robots policy could not be verified",
                fetch_status="BLOCKED",
            )
    else:
        def guard_naver_container(target: NormalizedUrl) -> None:
            if _naver_post_identity(target.canonical_url) != requested_naver_identity:
                raise FetchFailure(
                    "BLOCKED_NAVER_CONTAINER_REDIRECT",
                    "The Naver container redirected outside the requested post",
                    fetch_status="BLOCKED",
                )
            if not robots.can_fetch(
                session,
                target.canonical_url,
                user_agent=user_agent,
                timeout_seconds=timeout_seconds,
            ):
                raise FetchFailure(
                    "NAVER_CONTAINER_ROBOTS_BLOCKED",
                    "The Naver post container is disallowed by robots policy",
                    fetch_status="BLOCKED",
                )

        request_guard = guard_naver_container

    conditional_headers: dict[str, str] = {}
    if requested_naver_identity is None and candidate.etag:
        conditional_headers["If-None-Match"] = candidate.etag
    if requested_naver_identity is None and candidate.http_last_modified:
        conditional_headers["If-Modified-Since"] = candidate.http_last_modified

    response = request_with_redirects(
        session,
        candidate.canonical_url,
        user_agent=user_agent,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        extra_headers=conditional_headers,
        request_guard=request_guard,
    )
    if response.status_code == 304:
        if requested_naver_identity is not None:
            raise FetchFailure(
                "UNUSABLE_NAVER_CONTAINER_304",
                "A Naver container 304 cannot prove that iframe content is unchanged",
            )
        return response, None
    if requested_naver_identity is None:
        return response, _extract_html_response(response)

    # Exact Naver posts always use the restricted iframe path, even if the
    # outer shell itself contains enough menu text for generic extraction.
    try:
        _extract_html_response(response)
    except FetchFailure as error:
        if error.code != "INSUFFICIENT_TEXT":
            raise

    final_identity = _naver_post_identity(response.final_url)
    if final_identity != requested_naver_identity:
        raise FetchFailure(
            "BLOCKED_NAVER_CONTAINER_REDIRECT",
            "The Naver container redirected to a different post",
            fetch_status="BLOCKED",
        )
    iframe = _naver_iframe_target(
        response.final_url,
        decode_body(response.body, response.content_type),
    )
    if iframe is None:
        raise FetchFailure(
            "UNSUPPORTED_NAVER_MAIN_FRAME",
            "The exact Naver post did not expose one supported mainFrame",
            status_code=response.status_code,
            fetch_status="UNSUPPORTED",
            diagnostics=_response_diagnostics(response),
        )

    iframe_url, blog_id, log_no = iframe
    container = urlsplit(response.final_url)

    def guard_naver_content(target: NormalizedUrl) -> None:
        if not _matches_naver_content_target(
            target.canonical_url,
            blog_id=blog_id,
            log_no=log_no,
        ):
            raise FetchFailure(
                "BLOCKED_NAVER_IFRAME_REDIRECT",
                "The Naver content frame redirected outside the allowed post",
                fetch_status="BLOCKED",
            )
        if not robots.can_fetch(
            session,
            target.canonical_url,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
        ):
            raise FetchFailure(
                "NAVER_IFRAME_ROBOTS_BLOCKED",
                "The Naver content frame is disallowed by robots policy",
                fetch_status="BLOCKED",
            )

    remaining_bytes = max_bytes - len(response.body)
    if remaining_bytes <= 0:
        raise FetchFailure(
            "RESPONSE_TOO_LARGE",
            "The combined container and iframe response exceeded the limit",
            fetch_status="UNSUPPORTED",
            diagnostics=_response_diagnostics(response),
        )
    iframe_response = request_with_redirects(
        session,
        iframe_url,
        user_agent=user_agent,
        timeout_seconds=timeout_seconds,
        max_bytes=remaining_bytes,
        extra_headers={"Referer": response.final_url},
        request_guard=guard_naver_content,
    )
    try:
        iframe_page = _extract_html_response(
            iframe_response,
            extractor=extract_naver_post_page,
        )
    except FetchFailure as iframe_error:
        iframe_error.add_diagnostics(
            retrieval_mode=NAVER_IFRAME_MODE,
            container_host=(container.hostname or "").lower()[:253],
            container_path=(container.path or "/")[:1000],
        )
        raise

    content = urlsplit(iframe_response.final_url)
    retrieval = {
        "mode": NAVER_IFRAME_MODE,
        "depth": 1,
        "container_host": (container.hostname or "").lower()[:253],
        "container_path": (container.path or "/")[:1000],
        "content_host": (content.hostname or "").lower()[:253],
        "content_path": (content.path or "/")[:1000],
        "blog_id": blog_id,
        "log_no": log_no,
        "content_http_status": iframe_response.status_code,
        "content_type": (iframe_response.content_type or "")[:255],
        "response_bytes": len(iframe_response.body),
    }
    return response, replace(iframe_page, retrieval=retrieval)


def load_candidates(
    conn: psycopg.Connection[Any],
    *,
    hosts: Sequence[str],
    source_ids: Sequence[UUID] = (),
    limit: int,
    retry_failed: bool,
    refetch: bool = False,
) -> list[CrawlCandidate]:
    statuses = ["DISCOVERED"]
    if retry_failed:
        statuses.extend(["FAILED", "UNSUPPORTED", "BLOCKED"])
    if refetch:
        statuses.extend(["FETCHED", "NOT_MODIFIED"])

    params: list[Any] = [statuses]
    host_filter = ""
    if hosts:
        host_filter = "AND source.host = ANY(%s)"
        params.append([host.lower() for host in hosts])
    source_filter = ""
    if source_ids:
        source_filter = "AND source.source_id = ANY(%s)"
        params.append(list(source_ids))
    params.append(limit)

    sql = f"""
        SELECT
            source.source_id,
            source.canonical_url,
            source.host,
            source.etag,
            source.http_last_modified,
            MAX(
                (p.application_method IS NULL)::INTEGER
              + (p.screening_method IS NULL)::INTEGER
              + (p.submission_documents IS NULL)::INTEGER
              + (p.participation_exclusion IS NULL)::INTEGER
              + (p.additional_qualification IS NULL)::INTEGER
            )::INTEGER AS priority_score
        FROM web_source AS source
        JOIN policy_web_source AS link
          ON link.source_id = source.source_id
         AND link.is_active = TRUE
        JOIN policy AS p
          ON p.policy_no = link.policy_no
         AND p.is_active = TRUE
        WHERE source.fetch_status = ANY(%s)
          {host_filter}
          {source_filter}
        GROUP BY source.source_id
        ORDER BY priority_score DESC, source.host, source.source_id
        LIMIT %s
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [CrawlCandidate(**row) for row in cur.fetchall()]


def load_policy_evidence(
    conn: psycopg.Connection[Any],
    source_id: Any,
) -> list[PolicyEvidence]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT DISTINCT
                p.policy_no,
                p.policy_name,
                ARRAY_REMOVE(ARRAY[
                    p.supervising_org_name,
                    p.operating_org_name,
                    p.registrar_org_name
                ], NULL) AS organization_names,
                ARRAY(
                    SELECT DISTINCT resolved.region_name
                    FROM v_policy_region_resolved AS resolved
                    WHERE resolved.policy_no = p.policy_no
                      AND resolved.resolution_type <> 'UNRESOLVED'
                      AND resolved.is_active = TRUE
                    ORDER BY resolved.region_name
                ) AS region_names,
                ARRAY(
                    SELECT TO_CHAR(period.start_date, 'YYYY-MM-DD')
                           || ' ~ '
                           || TO_CHAR(period.end_date, 'YYYY-MM-DD')
                    FROM policy_application_period AS period
                    WHERE period.policy_no = p.policy_no
                    ORDER BY period.period_index
                ) AS application_periods,
                COALESCE(p.keywords, '{}'::TEXT[])
                  || COALESCE(p.middle_categories, '{}'::TEXT[])
                    AS distinctive_terms
            FROM policy_web_source AS link
            JOIN policy AS p
              ON p.policy_no = link.policy_no
            WHERE link.source_id = %s
              AND link.is_active = TRUE
              AND p.is_active = TRUE
            ORDER BY p.policy_no
            """,
            (source_id,),
        )
        return [PolicyEvidence.from_mapping(row) for row in cur.fetchall()]


def store_failure(
    conn: psycopg.Connection[Any],
    candidate: CrawlCandidate,
    error: FetchFailure,
) -> None:
    invalidate_current_document = (
        error.fetch_status in {"BLOCKED", "UNSUPPORTED"}
        or error.status_code in {404, 410}
    )
    diagnostics = {
        **error.diagnostics,
        "error_code": error.code,
        "stale_document_invalidated": invalidate_current_document,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE web_source
            SET fetch_status = %s,
                http_status = %s,
                last_attempt_at = NOW(),
                error_type = %s,
                error_message = %s,
                metadata = COALESCE(metadata, '{}'::jsonb)
                    || jsonb_build_object('last_failure_diagnostics', %s)
            WHERE source_id = %s
            """,
            (
                error.fetch_status,
                error.status_code,
                error.code,
                str(error)[:1000],
                Jsonb(diagnostics),
                candidate.source_id,
            ),
        )
        if invalidate_current_document:
            # A terminal safety/content failure means the previously fetched
            # body can no longer be asserted as this URL's current document.
            # Keep historical rows, but remove them from every verified view
            # and force any future successful version through validation again.
            cur.execute(
                """
                UPDATE web_document
                SET is_current = FALSE
                WHERE source_id = %s
                  AND is_current = TRUE
                """,
                (candidate.source_id,),
            )
            cur.execute(
                """
                UPDATE policy_web_source
                SET validation_status = 'PENDING',
                    validation_score = NULL,
                    matched_signals = '[]'::jsonb,
                    conflicting_signals = '[]'::jsonb,
                    validation_reason = NULL,
                    validator_version = NULL,
                    validated_at = NULL,
                    validated_document_id = NULL,
                    reviewed_by = NULL,
                    reviewed_at = NULL
                WHERE source_id = %s
                  AND is_active = TRUE
                  AND (
                      validated_document_id IS NOT NULL
                      OR validation_status <> 'PENDING'
                      OR reviewed_at IS NOT NULL
                  )
                """,
                (candidate.source_id,),
            )
    conn.commit()


def store_not_modified(
    conn: psycopg.Connection[Any],
    candidate: CrawlCandidate,
    response: HttpPayload,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE web_source
            SET fetch_status = 'NOT_MODIFIED',
                final_url = %s,
                http_status = 304,
                last_attempt_at = NOW(),
                last_success_at = NOW(),
                error_type = NULL,
                error_message = NULL,
                metadata = COALESCE(metadata, '{}'::jsonb)
                    - 'last_failure_diagnostics'
            WHERE source_id = %s
            """,
            (response.final_url, candidate.source_id),
        )
    conn.commit()


def store_link_validations(
    conn: psycopg.Connection[Any],
    *,
    source_id: Any,
    document_id: Any,
    final_url: str,
    http_status: int | None,
    extracted: ExtractedPage,
    policies: Iterable[PolicyEvidence],
) -> dict[str, int]:
    """Validate policy/source links against one exact document version.

    The caller owns the transaction. Binding every verdict to document_id is
    what prevents a later, changed page from inheriting an older VERIFIED
    result merely because its URL stayed the same.
    """
    validation_counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for policy in policies:
            result = validate_policy_source(
                policy,
                PageEvidence(
                    final_url=final_url,
                    title=extracted.title,
                    h1=extracted.h1,
                    headings=extracted.headings,
                    text=extracted.text,
                    http_status=http_status,
                ),
            )
            if (
                extracted.retrieval.get("mode") == NAVER_IFRAME_MODE
                and result.status == VERIFIED
            ):
                # The outer Naver URL is exact and the iframe identity is
                # locked to it, but this adapter is still new and the policy
                # link must be inspected by a person before becoming RAG
                # evidence. This is intentionally stricter than the generic
                # deterministic validator.
                result = replace(
                    result,
                    status=REVIEW_REQUIRED,
                    score=min(result.score, 0.79),
                    matched_signals=tuple(
                        dict.fromkeys(
                            (*result.matched_signals, "naver_iframe_manual_review")
                        )
                    ),
                    reason=(
                        f"{result.reason} "
                        "An exact Naver iframe document requires manual review "
                        "before it can become RAG evidence."
                    ),
                )
            validation_counts[result.status] = (
                validation_counts.get(result.status, 0) + 1
            )
            cur.execute(
                """
                UPDATE policy_web_source
                SET validation_status = %s,
                    validation_score = %s,
                    matched_signals = %s,
                    conflicting_signals = %s,
                    validation_reason = %s,
                    validator_version = %s,
                    validated_at = NOW(),
                    validated_document_id = %s
                WHERE policy_no = %s
                  AND source_id = %s
                  AND is_active = TRUE
                  AND reviewed_at IS NULL
                """,
                (
                    result.status,
                    result.score,
                    Jsonb(list(result.matched_signals)),
                    Jsonb(list(result.conflicting_signals)),
                    result.reason,
                    result.validator_version,
                    document_id,
                    policy.policy_no,
                    source_id,
                ),
            )
    return validation_counts


def store_success(
    conn: psycopg.Connection[Any],
    candidate: CrawlCandidate,
    response: HttpPayload,
    extracted: ExtractedPage,
    policies: Iterable[PolicyEvidence],
) -> dict[str, int]:
    iframe_retrieval = extracted.retrieval.get("mode") == NAVER_IFRAME_MODE
    source_etag = None if iframe_retrieval else response.headers.get("etag")
    source_last_modified = (
        None if iframe_retrieval else response.headers.get("last-modified")
    )
    document_metadata: dict[str, Any] = {
        "h1": extracted.h1,
        "headings": list(extracted.headings),
    }
    if extracted.retrieval:
        document_metadata["retrieval"] = extracted.retrieval

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE web_source
                SET fetch_status = 'FETCHED',
                    final_url = %s,
                    http_status = %s,
                    content_type = %s,
                    etag = %s,
                    http_last_modified = %s,
                    last_attempt_at = NOW(),
                    last_success_at = NOW(),
                    error_type = NULL,
                    error_message = NULL,
                    metadata = COALESCE(metadata, '{}'::jsonb)
                        - 'last_failure_diagnostics'
                WHERE source_id = %s
                """,
                (
                    response.final_url,
                    response.status_code,
                    response.content_type,
                    source_etag,
                    source_last_modified,
                    candidate.source_id,
                ),
            )
            cur.execute(
                """
                UPDATE web_document
                SET is_current = FALSE
                WHERE source_id = %s
                  AND is_current = TRUE
                """,
                (candidate.source_id,),
            )
            cur.execute(
                """
                INSERT INTO web_document (
                    source_id,
                    title,
                    extracted_text,
                    content_hash,
                    extraction_method,
                    extraction_version,
                    is_current,
                    metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
                ON CONFLICT (source_id, content_hash) DO UPDATE
                SET title = EXCLUDED.title,
                    extracted_text = EXCLUDED.extracted_text,
                    extraction_method = EXCLUDED.extraction_method,
                    extraction_version = EXCLUDED.extraction_version,
                    fetched_at = NOW(),
                    is_current = TRUE,
                    metadata = EXCLUDED.metadata
                RETURNING document_id
                """,
                (
                    candidate.source_id,
                    extracted.title,
                    extracted.text,
                    extracted.content_hash,
                    EXTRACTOR_NAME,
                    EXTRACTOR_VERSION,
                    Jsonb(document_metadata),
                ),
            )
            document_id = cur.fetchone()[0]

            # The variable is deliberately used so a failed RETURNING cannot
            # silently commit a source without a document.
            if document_id is None:
                raise RuntimeError("web_document UPSERT returned no document_id")

            # A manual or automatic verdict is valid only for the exact
            # document version that was inspected. If the body hash changed,
            # clear the old verdict so the new current document must pass the
            # validator and, where required, manual review again.
            cur.execute(
                """
                UPDATE policy_web_source
                SET validation_status = 'PENDING',
                    validation_score = NULL,
                    matched_signals = '[]'::jsonb,
                    conflicting_signals = '[]'::jsonb,
                    validation_reason = NULL,
                    validator_version = NULL,
                    validated_at = NULL,
                    validated_document_id = NULL,
                    reviewed_by = NULL,
                    reviewed_at = NULL
                WHERE source_id = %s
                  AND is_active = TRUE
                  AND validated_document_id IS DISTINCT FROM %s
                """,
                (candidate.source_id, document_id),
            )
        validation_counts = store_link_validations(
            conn,
            source_id=candidate.source_id,
            document_id=document_id,
            final_url=response.final_url,
            http_status=response.status_code,
            extracted=extracted,
            policies=policies,
        )
    return validation_counts


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or crawl a small set of discovered policy web sources. "
            "Without --write no network request is made."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="fetch and store selected sources (default: plan only)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum sources to select (default: 10)",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="restrict to an exact normalized host; may be repeated",
    )
    parser.add_argument(
        "--source-id",
        action="append",
        type=UUID,
        default=[],
        help="restrict to an exact source UUID; may be repeated",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="also select FAILED, UNSUPPORTED, and BLOCKED sources",
    )
    parser.add_argument(
        "--refetch",
        action="store_true",
        help="also select FETCHED and NOT_MODIFIED sources for re-extraction",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help="seconds between source requests (default from env, normally 2)",
    )
    args = parser.parse_args(argv)
    validate_positive(args.limit, "limit")
    validate_non_negative(args.request_delay, "request delay")
    if not DEFAULT_USER_AGENT:
        parser.error("YOUTHLINK_CRAWLER_USER_AGENT must not be blank")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_positive(DEFAULT_TIMEOUT_SECONDS, "crawler timeout")
    validate_positive(DEFAULT_MAX_RESPONSE_BYTES, "crawler max response bytes")

    with psycopg.connect(DB_DSN) as conn:
        candidates = load_candidates(
            conn,
            hosts=args.host,
            source_ids=args.source_id,
            limit=args.limit,
            retry_failed=args.retry_failed,
            refetch=args.refetch,
        )
        print(f"crawl plan: sources={len(candidates)}")
        for candidate in candidates:
            print(
                f"{candidate.source_id} host={candidate.host} "
                f"missing_priority={candidate.priority_score}"
            )
        if not args.write or not candidates:
            if not args.write:
                print("plan only; add --write to make network requests")
            return 0

        # Candidate selection is read-only; close that transaction so each
        # source result below can commit or roll back independently.
        conn.commit()

        session = requests.Session()
        session.trust_env = False
        robots = RobotsCache()
        succeeded = 0
        failed = 0
        not_modified = 0
        validation_totals: dict[str, int] = {}
        try:
            for index, candidate in enumerate(candidates, start=1):
                try:
                    response, extracted = fetch_html_page(
                        session,
                        robots,
                        candidate,
                        user_agent=DEFAULT_USER_AGENT,
                        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                        max_bytes=DEFAULT_MAX_RESPONSE_BYTES,
                    )
                    if extracted is None:
                        store_not_modified(conn, candidate, response)
                        not_modified += 1
                        print(f"[{index}/{len(candidates)}] not modified {candidate.host}")
                    else:
                        policies = load_policy_evidence(conn, candidate.source_id)
                        conn.commit()
                        counts = store_success(
                            conn,
                            candidate,
                            response,
                            extracted,
                            policies,
                        )
                        succeeded += 1
                        for status, count in counts.items():
                            validation_totals[status] = (
                                validation_totals.get(status, 0) + count
                            )
                        print(
                            f"[{index}/{len(candidates)}] fetched {candidate.host} "
                            f"validations={json.dumps(counts, ensure_ascii=False, sort_keys=True)}"
                        )
                except FetchFailure as error:
                    conn.rollback()
                    store_failure(conn, candidate, error)
                    failed += 1
                    print(
                        f"[{index}/{len(candidates)}] {error.fetch_status} "
                        f"{candidate.host} code={error.code}"
                    )

                if index < len(candidates) and args.request_delay:
                    time.sleep(args.request_delay)
        finally:
            session.close()

    print(
        "crawl complete: "
        f"fetched={succeeded}, not_modified={not_modified}, failed={failed}, "
        f"validation={json.dumps(validation_totals, ensure_ascii=False, sort_keys=True)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
