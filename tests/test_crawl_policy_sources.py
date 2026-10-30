from __future__ import annotations

import socket
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID

from etl.crawl_policy_sources import (
    CrawlCandidate,
    FetchFailure,
    HttpPayload,
    RobotsCache,
    _read_bounded_response,
    extract_page,
    fetch_html_page,
    parse_args,
    request_with_redirects,
    resolve_public_addresses,
    store_failure,
    store_link_validations,
    store_not_modified,
    store_success,
)
from etl.source_relevance import PolicyEvidence
from etl.sync_policy_sources import normalize_url


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self._chunks


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requested_urls: list[str] = []

    def get(self, url: str, **kwargs):
        del kwargs
        self.requested_urls.append(url)
        return self.responses.pop(0)


class AllowRobots:
    def can_fetch(self, *args, **kwargs) -> bool:
        del args, kwargs
        return True


class RecordingRobots:
    def __init__(self, *, deny_iframe: bool = False) -> None:
        self.deny_iframe = deny_iframe
        self.urls: list[str] = []

    def can_fetch(self, session, url, **kwargs) -> bool:
        del session, kwargs
        self.urls.append(url)
        if self.deny_iframe and "PostView.naver" in url:
            return False
        return True


class RecordingSuccessCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, object]] = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        self._row = ("document-1",) if "RETURNING document_id" in sql else None

    def fetchone(self):
        return self._row


class RecordingSuccessConnection:
    def __init__(self) -> None:
        self.recording_cursor = RecordingSuccessCursor()

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class AddressSafetyTests(unittest.TestCase):
    def test_accepts_only_globally_routable_dns_answers(self) -> None:
        target = normalize_url("https://example.com/policy")
        answer = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("8.8.8.8", 443),
        )

        with patch("etl.crawl_policy_sources.socket.getaddrinfo", return_value=[answer]):
            self.assertEqual(resolve_public_addresses(target), ("8.8.8.8",))

    def test_blocks_host_if_any_dns_answer_is_non_public(self) -> None:
        target = normalize_url("https://example.com/policy")
        answers = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            ),
        ]

        with patch("etl.crawl_policy_sources.socket.getaddrinfo", return_value=answers):
            with self.assertRaises(FetchFailure) as raised:
                resolve_public_addresses(target)

        self.assertEqual(raised.exception.code, "BLOCKED_ADDRESS")
        self.assertEqual(raised.exception.fetch_status, "BLOCKED")

    def test_dns_os_error_is_isolated_as_fetch_failure(self) -> None:
        target = normalize_url("https://example.com/policy")

        with patch(
            "etl.crawl_policy_sources.socket.getaddrinfo",
            side_effect=OSError("resolver unavailable"),
        ):
            with self.assertRaises(FetchFailure) as raised:
                resolve_public_addresses(target)

        self.assertEqual(raised.exception.code, "DNS_ERROR")
        self.assertNotIn("resolver unavailable", str(raised.exception))

    def test_private_redirect_is_reported_as_blocked_fetch_failure(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "http://127.0.0.1/admin"},
                )
            ]
        )

        with patch("etl.crawl_policy_sources.resolve_public_addresses"):
            with self.assertRaises(FetchFailure) as raised:
                request_with_redirects(
                    session,
                    "https://example.com/policy",
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=1024,
                )

        self.assertEqual(raised.exception.fetch_status, "BLOCKED")

    def test_request_guard_runs_before_every_redirect_hop(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    status_code=302,
                    headers={"Location": "/second"},
                ),
                FakeResponse(
                    status_code=200,
                    headers={"Content-Type": "text/html"},
                    chunks=(b"<html></html>",),
                ),
            ]
        )
        guarded_urls: list[str] = []

        with patch("etl.crawl_policy_sources.resolve_public_addresses"):
            request_with_redirects(
                session,
                "https://example.com/first",
                user_agent="YouthLinkTest/1.0",
                timeout_seconds=1,
                max_bytes=1024,
                request_guard=lambda target: guarded_urls.append(
                    target.canonical_url
                ),
            )

        self.assertEqual(
            guarded_urls,
            ["https://example.com/first", "https://example.com/second"],
        )


class RobotsSafetyTests(unittest.TestCase):
    def test_missing_robots_file_allows_fetch(self) -> None:
        response = HttpPayload(
            final_url="https://example.com/robots.txt",
            status_code=404,
            content_type="text/plain",
            headers={},
            body=b"",
        )
        robots = RobotsCache()

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=response,
        ):
            allowed = robots.can_fetch(
                FakeSession([]),
                "https://example.com/policy/1",
                user_agent="YouthLinkTest/1.0",
                timeout_seconds=1,
            )

        self.assertTrue(allowed)

    def test_explicit_robots_rule_blocks_fetch(self) -> None:
        response = HttpPayload(
            final_url="https://example.com/robots.txt",
            status_code=200,
            content_type="text/plain; charset=utf-8",
            headers={},
            body=b"User-agent: *\nDisallow: /policy\n",
        )
        robots = RobotsCache()

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=response,
        ):
            allowed = robots.can_fetch(
                FakeSession([]),
                "https://example.com/policy/1",
                user_agent="YouthLinkTest/1.0",
                timeout_seconds=1,
            )

        self.assertFalse(allowed)

    def test_robots_lookup_failure_blocks_fetch_conservatively(self) -> None:
        robots = RobotsCache()

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            side_effect=FetchFailure("NETWORK_ERROR", "network failed"),
        ):
            allowed = robots.can_fetch(
                FakeSession([]),
                "https://example.com/policy/1",
                user_agent="YouthLinkTest/1.0",
                timeout_seconds=1,
            )

        self.assertFalse(allowed)


class ExtractionSafetyTests(unittest.TestCase):
    def test_prefers_article_and_removes_navigation_text(self) -> None:
        article_text = "지원 대상 신청 방법 제출 서류와 선정 기준을 안내합니다. " * 12
        navigation_text = "관련 없는 다른 정책 목록 " * 40
        page = extract_page(
            "<html><head><title>청년 지원 정책</title></head><body>"
            f"<nav>{navigation_text}</nav>"
            "<article><h1>청년 주거 지원</h1>"
            f"<p>{article_text}</p></article>"
            "</body></html>"
        )

        self.assertIn("지원 대상 신청 방법", page.text)
        self.assertNotIn("관련 없는 다른 정책", page.text)
        self.assertEqual(page.h1, "청년 주거 지원")

    def test_ignores_heading_from_global_site_header(self) -> None:
        article_text = "실제 게시물의 지원 대상과 접수 방법을 설명합니다. " * 12
        page = extract_page(
            "<html><head><title>정책 게시판</title></head><body>"
            "<header><h1>관련 없는 정책명</h1></header>"
            "<article><h2>모집 안내</h2>"
            f"<p>{article_text}</p></article>"
            "</body></html>"
        )

        self.assertEqual(page.h1, "")
        self.assertEqual(page.headings, ("모집 안내",))
        self.assertNotIn("관련 없는 정책명", page.text)

    def test_rejects_page_with_too_little_meaningful_text(self) -> None:
        with self.assertRaises(FetchFailure) as raised:
            extract_page("<html><body><main>짧은 안내</main></body></html>")

        self.assertEqual(raised.exception.code, "INSUFFICIENT_TEXT")
        self.assertEqual(raised.exception.fetch_status, "UNSUPPORTED")

    def test_insufficient_text_records_counts_without_raw_content(self) -> None:
        secret = "RAW_BODY_MUST_NOT_BE_STORED"
        html = (
            "<html><head><title>짧은 제목</title>"
            f"<script>{secret}</script></head><body>"
            '<iframe src="/embedded"></iframe>'
            "<main>짧은 안내</main></body></html>"
        )

        with self.assertRaises(FetchFailure) as raised:
            extract_page(html)

        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["script_count"], 1)
        self.assertEqual(diagnostics["iframe_count"], 1)
        self.assertGreaterEqual(diagnostics["semantic_candidate_count"], 1)
        self.assertGreater(diagnostics["body_text_chars"], 0)
        self.assertGreater(diagnostics["selected_text_chars"], 0)
        self.assertEqual(diagnostics["title_chars"], len("짧은 제목"))
        self.assertNotIn(secret, repr(diagnostics))

    def test_fetch_failure_adds_safe_response_diagnostics(self) -> None:
        secret_query = "token=QUERY_SECRET_MUST_NOT_BE_STORED"
        final_url = f"https://example.com/detail/1?{secret_query}"
        body = (
            "<html><head><script>clientBootstrap()</script></head>"
            "<body><main>짧은 안내</main></body></html>"
        ).encode("utf-8")
        response = HttpPayload(
            final_url=final_url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            headers={},
            body=body,
        )
        candidate = CrawlCandidate(
            source_id="source-1",
            canonical_url="https://example.com/detail/1",
            host="example.com",
            etag=None,
            http_last_modified=None,
            priority_score=5,
        )

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=response,
        ):
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    FakeSession([]),
                    AllowRobots(),
                    candidate,
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=1024,
                )

        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["http_status"], 200)
        self.assertEqual(diagnostics["content_type"], "text/html; charset=utf-8")
        self.assertEqual(diagnostics["response_bytes"], len(body))
        self.assertEqual(diagnostics["final_host"], "example.com")
        self.assertEqual(diagnostics["final_path"], "/detail/1")
        self.assertNotIn(secret_query, repr(diagnostics))


class NaverIframeSafetyTests(unittest.TestCase):
    @staticmethod
    def candidate(
        url: str = "https://blog.naver.com/youthcenter/223456789012",
    ) -> CrawlCandidate:
        normalized = normalize_url(url)
        return CrawlCandidate(
            source_id="source-naver",
            canonical_url=normalized.canonical_url,
            host=normalized.host,
            etag=None,
            http_last_modified=None,
            priority_score=5,
        )

    @staticmethod
    def outer_response(iframe_src: str) -> HttpPayload:
        body = (
            "<html><head><title>네이버 블로그</title></head><body>"
            f"<div>{'OUTER_SHELL_MENU ' * 30}</div>"
            f'<iframe id="mainFrame" src="{iframe_src}"></iframe>'
            "</body></html>"
        ).encode("utf-8")
        return HttpPayload(
            final_url="https://blog.naver.com/youthcenter/223456789012",
            status_code=200,
            content_type="text/html; charset=utf-8",
            headers={},
            body=body,
        )

    @staticmethod
    def content_response(
        *,
        final_url: str = (
            "https://blog.naver.com/PostView.naver?"
            "blogId=youthcenter&logNo=223456789012"
        ),
        enough_text: bool = True,
        nested_iframe: bool = False,
    ) -> HttpPayload:
        text = (
            "청년 지원사업의 지원 대상과 신청 방법, 제출 서류를 안내합니다. " * 15
            if enough_text
            else "짧은 안내"
        )
        nested = (
            '<iframe id="mainFrame" src="/PostView.naver?blogId=other&logNo=2">'
            "</iframe>"
            if nested_iframe
            else ""
        )
        body = (
            "<html><head><title>청년 지원사업 모집 공고</title></head>"
            "<body><div class='blog-navigation'>관련 없는 블로그 메뉴</div>"
            "<div class='se-main-container'>"
            f"<h1>청년 지원사업</h1><p>{text}</p>{nested}</div>"
            "<footer>관련 없는 댓글과 이웃 글</footer></body></html>"
        ).encode("utf-8")
        return HttpPayload(
            final_url=final_url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            headers={},
            body=body,
        )

    @staticmethod
    def guarded_side_effect(responses: list[HttpPayload]):
        def respond(session, url, **kwargs):
            del session
            guard = kwargs.get("request_guard")
            if guard is not None:
                guard(normalize_url(url))
            return responses.pop(0)

        return respond

    def test_follows_one_allowed_naver_main_frame(self) -> None:
        iframe_url = (
            "https://blog.naver.com/PostView.naver?"
            "blogId=youthcenter&logNo=223456789012"
        )
        robots = RecordingRobots()
        responses = [
            self.outer_response(
                "/PostView.naver?blogId=youthcenter&logNo=223456789012"
            ),
            self.content_response(final_url=iframe_url),
        ]
        outer_bytes = len(responses[0].body)

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            side_effect=self.guarded_side_effect(responses),
        ) as request:
            response, page = fetch_html_page(
                FakeSession([]),
                robots,
                self.candidate(),
                user_agent="YouthLinkTest/1.0",
                timeout_seconds=1,
                max_bytes=10_000,
            )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].kwargs["max_bytes"],
            10_000 - outer_bytes,
        )
        self.assertEqual(robots.urls, [self.candidate().canonical_url, iframe_url])
        self.assertEqual(response.final_url, self.candidate().canonical_url)
        self.assertIsNotNone(page)
        self.assertIn("지원 대상과 신청 방법", page.text)
        self.assertNotIn("OUTER_SHELL_MENU", page.text)
        self.assertNotIn("관련 없는 블로그 메뉴", page.text)
        self.assertNotIn("관련 없는 댓글과 이웃 글", page.text)
        self.assertEqual(page.retrieval["mode"], "NAVER_POST_IFRAME")
        self.assertEqual(page.retrieval["depth"], 1)
        self.assertEqual(page.retrieval["container_host"], "blog.naver.com")
        self.assertEqual(
            page.retrieval["container_path"],
            "/youthcenter/223456789012",
        )
        self.assertEqual(page.retrieval["content_host"], "blog.naver.com")
        self.assertEqual(page.retrieval["content_path"], "/PostView.naver")
        self.assertEqual(page.retrieval["blog_id"], "youthcenter")
        self.assertEqual(page.retrieval["log_no"], "223456789012")
        self.assertEqual(page.retrieval["content_http_status"], 200)
        self.assertEqual(page.retrieval["content_type"], "text/html; charset=utf-8")
        self.assertGreater(page.retrieval["response_bytes"], 0)
        self.assertNotIn("?", repr(page.retrieval))

    def test_never_follows_foreign_iframe_from_naver_container(self) -> None:
        outer = self.outer_response("https://tracker.example/collect?secret=1")

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=outer,
        ) as request:
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    FakeSession([]),
                    RecordingRobots(),
                    self.candidate(),
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(raised.exception.code, "BLOCKED_NAVER_IFRAME_TARGET")
        self.assertEqual(request.call_count, 1)

    def test_non_naver_source_never_follows_iframe(self) -> None:
        outer = HttpPayload(
            final_url="https://example.org/policy",
            status_code=200,
            content_type="text/html; charset=utf-8",
            headers={},
            body=(
                '<html><body><iframe src="/detail"></iframe></body></html>'
            ).encode("utf-8"),
        )
        candidate = self.candidate("https://example.org/policy")

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=outer,
        ) as request:
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    FakeSession([]),
                    RecordingRobots(),
                    candidate,
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(request.call_count, 1)

    def test_naver_blog_root_is_not_eligible_for_iframe_follow(self) -> None:
        candidate = self.candidate("https://blog.naver.com/youthcenter")
        outer = HttpPayload(
            final_url=candidate.canonical_url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            headers={},
            body=(
                "<html><body><iframe id=\"mainFrame\" "
                "src=\"/PostView.naver?blogId=youthcenter&logNo=2\">"
                "</iframe></body></html>"
            ).encode("utf-8"),
        )

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=outer,
        ) as request:
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    FakeSession([]),
                    RecordingRobots(),
                    candidate,
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(raised.exception.code, "UNSUPPORTED_NAVER_NON_POST_URL")
        self.assertEqual(request.call_count, 0)

    def test_naver_iframe_identity_must_match_outer_post(self) -> None:
        outer = self.outer_response(
            "/PostView.naver?blogId=anotherBlog&logNo=999999999999"
        )

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=outer,
        ) as request:
            with self.assertRaises(FetchFailure):
                fetch_html_page(
                    FakeSession([]),
                    RecordingRobots(),
                    self.candidate(),
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(request.call_count, 1)

    def test_naver_outer_redirect_must_not_change_post_identity(self) -> None:
        outer = replace(
            self.outer_response(
                "/PostView.naver?blogId=other&logNo=999999999999"
            ),
            final_url="https://blog.naver.com/other/999999999999",
        )

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            return_value=outer,
        ) as request:
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    FakeSession([]),
                    RecordingRobots(),
                    self.candidate(),
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(raised.exception.code, "BLOCKED_NAVER_CONTAINER_REDIRECT")
        self.assertEqual(request.call_count, 1)

    def test_naver_outer_redirect_is_blocked_before_second_request(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    status_code=302,
                    headers={
                        "Location": "https://blog.naver.com/other/999999999999"
                    },
                )
            ]
        )
        robots = RecordingRobots()

        with patch("etl.crawl_policy_sources.resolve_public_addresses"):
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    session,
                    robots,
                    self.candidate(),
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(raised.exception.code, "BLOCKED_NAVER_CONTAINER_REDIRECT")
        self.assertEqual(
            session.requested_urls,
            [self.candidate().canonical_url],
        )
        self.assertEqual(robots.urls, [self.candidate().canonical_url])

    def test_naver_iframe_requires_https_and_default_port(self) -> None:
        targets = (
            "http://blog.naver.com/PostView.naver?"
            "blogId=youthcenter&logNo=223456789012",
            "https://blog.naver.com:444/PostView.naver?"
            "blogId=youthcenter&logNo=223456789012",
        )
        for target in targets:
            with self.subTest(target=target), patch(
                "etl.crawl_policy_sources.request_with_redirects",
                return_value=self.outer_response(target),
            ) as request:
                with self.assertRaises(FetchFailure) as raised:
                    fetch_html_page(
                        FakeSession([]),
                        RecordingRobots(),
                        self.candidate(),
                        user_agent="YouthLinkTest/1.0",
                        timeout_seconds=1,
                        max_bytes=10_000,
                    )

                self.assertEqual(raised.exception.code, "BLOCKED_NAVER_IFRAME_TARGET")
                self.assertEqual(request.call_count, 1)

    def test_rechecks_robots_before_fetching_iframe(self) -> None:
        iframe_url = (
            "https://blog.naver.com/PostView.naver?"
            "blogId=youthcenter&logNo=223456789012"
        )
        robots = RecordingRobots(deny_iframe=True)
        outer = self.outer_response(
            "/PostView.naver?blogId=youthcenter&logNo=223456789012"
        )

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            side_effect=self.guarded_side_effect([outer]),
        ) as request:
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    FakeSession([]),
                    robots,
                    self.candidate(),
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(robots.urls, [self.candidate().canonical_url, iframe_url])
        self.assertEqual(raised.exception.fetch_status, "BLOCKED")

    def test_does_not_follow_nested_iframe_beyond_one_level(self) -> None:
        outer = self.outer_response(
            "/PostView.naver?blogId=youthcenter&logNo=223456789012"
        )
        inner = self.content_response(
            enough_text=False,
            nested_iframe=True,
        )

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            side_effect=[outer, inner],
        ) as request:
            with self.assertRaises(FetchFailure):
                fetch_html_page(
                    FakeSession([]),
                    RecordingRobots(),
                    self.candidate(),
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertEqual(request.call_count, 2)

    def test_failed_iframe_diagnostics_never_store_body_or_query(self) -> None:
        secret = "SECRET_IFRAME_QUERY_VALUE"
        iframe_src = f"/PostView.naver?blogId=youthcenter&token={secret}"
        iframe_url = f"https://blog.naver.com{iframe_src}"
        outer = self.outer_response(iframe_src)
        inner = self.content_response(
            final_url=iframe_url,
            enough_text=False,
        )

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            side_effect=[outer, inner],
        ):
            with self.assertRaises(FetchFailure) as raised:
                fetch_html_page(
                    FakeSession([]),
                    RecordingRobots(),
                    self.candidate(),
                    user_agent="YouthLinkTest/1.0",
                    timeout_seconds=1,
                    max_bytes=10_000,
                )

        self.assertNotIn(secret, repr(raised.exception.diagnostics))
        self.assertNotIn("짧은 안내", repr(raised.exception.diagnostics))

    def test_store_success_preserves_query_free_iframe_provenance(self) -> None:
        iframe_url = (
            "https://blog.naver.com/PostView.naver?"
            "blogId=youthcenter&logNo=223456789012"
        )
        outer = replace(
            self.outer_response(
                "/PostView.naver?blogId=youthcenter&logNo=223456789012"
            ),
            headers={
                "etag": "container-etag-must-not-be-reused",
                "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            },
        )
        inner = self.content_response(final_url=iframe_url)
        candidate = self.candidate()

        with patch(
            "etl.crawl_policy_sources.request_with_redirects",
            side_effect=[outer, inner],
        ):
            response, page = fetch_html_page(
                FakeSession([]),
                RecordingRobots(),
                candidate,
                user_agent="YouthLinkTest/1.0",
                timeout_seconds=1,
                max_bytes=10_000,
            )

        conn = RecordingSuccessConnection()
        counts = store_success(
            conn,
            candidate,
            response,
            page,
            policies=(),
        )

        self.assertEqual(counts, {})
        web_source_params = next(
            params
            for sql, params in conn.recording_cursor.executions
            if "UPDATE web_source" in sql
        )
        self.assertIsNone(web_source_params[3])
        self.assertIsNone(web_source_params[4])
        document_params = next(
            params
            for sql, params in conn.recording_cursor.executions
            if "INSERT INTO web_document" in sql
        )
        metadata = document_params[-1].obj
        self.assertEqual(metadata["retrieval"], page.retrieval)
        self.assertNotIn("?", repr(metadata["retrieval"]))
        reset_sql = next(
            sql
            for sql, _ in conn.recording_cursor.executions
            if "UPDATE policy_web_source" in sql
            and "validated_document_id IS DISTINCT FROM" in sql
        )
        self.assertIn("reviewed_at = NULL", reset_sql)


class ResponseLimitTests(unittest.TestCase):
    def test_stops_reading_when_response_exceeds_limit(self) -> None:
        response = FakeResponse(
            status_code=200,
            chunks=(b"123456", b"789012"),
        )

        with self.assertRaises(FetchFailure) as raised:
            _read_bounded_response(response, max_bytes=10)

        self.assertEqual(raised.exception.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(raised.exception.fetch_status, "UNSUPPORTED")


class CrawlCliTests(unittest.TestCase):
    def test_source_id_filter_is_repeatable_and_typed(self) -> None:
        first = "15779e2f-931d-416d-bfde-40b3bedbdfb5"
        second = "6f135dcc-888a-4c07-9270-6ab676c35c5a"

        args = parse_args(
            [
                "--source-id",
                first,
                "--source-id",
                second,
                "--retry-failed",
                "--refetch",
            ]
        )

        self.assertEqual(args.source_id, [UUID(first), UUID(second)])
        self.assertTrue(args.retry_failed)
        self.assertTrue(args.refetch)


class RecordingWriteCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))


class RecordingWriteConnection:
    def __init__(self) -> None:
        self.recording_cursor = RecordingWriteCursor()
        self.commit_count = 0

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor

    def commit(self) -> None:
        self.commit_count += 1


class FailureDiagnosticsPersistenceTests(unittest.TestCase):
    def test_store_failure_writes_safe_diagnostics_only_to_web_source(self) -> None:
        conn = RecordingWriteConnection()
        candidate = CrawlCandidate(
            source_id="source-1",
            canonical_url="https://example.com/detail/1?secret=not-stored",
            host="example.com",
            etag=None,
            http_last_modified=None,
            priority_score=5,
        )
        error = FetchFailure(
            "INSUFFICIENT_TEXT",
            "The extracted page text is too short for policy validation",
            status_code=200,
            fetch_status="UNSUPPORTED",
            diagnostics={
                "response_bytes": 321,
                "final_host": "example.com",
                "final_path": "/detail/1",
                "selected_text_chars": 12,
            },
        )

        store_failure(conn, candidate, error)

        self.assertEqual(conn.commit_count, 1)
        sql, params = conn.recording_cursor.executions[0]
        self.assertIn("UPDATE web_source", sql)
        self.assertIn("last_failure_diagnostics", sql)
        self.assertNotIn("UPDATE policy\n", sql)
        self.assertNotIn("policy_chunk", sql)
        json_values = [
            value.obj for value in params if hasattr(value, "obj")
        ]
        self.assertEqual(len(json_values), 1)
        self.assertEqual(json_values[0]["response_bytes"], 321)
        self.assertTrue(json_values[0]["stale_document_invalidated"])
        self.assertNotIn("secret=not-stored", repr(json_values[0]))
        all_sql = "\n".join(sql for sql, _ in conn.recording_cursor.executions)
        self.assertIn("UPDATE web_document", all_sql)
        self.assertIn("UPDATE policy_web_source", all_sql)
        self.assertIn("reviewed_at = NULL", all_sql)

    def test_transient_failure_keeps_last_current_document(self) -> None:
        conn = RecordingWriteConnection()
        candidate = CrawlCandidate(
            source_id="source-1",
            canonical_url="https://example.com/detail/1",
            host="example.com",
            etag=None,
            http_last_modified=None,
            priority_score=5,
        )
        error = FetchFailure("NETWORK_ERROR", "temporary network failure")

        store_failure(conn, candidate, error)

        self.assertEqual(len(conn.recording_cursor.executions), 1)
        _, params = conn.recording_cursor.executions[0]
        diagnostics = next(value.obj for value in params if hasattr(value, "obj"))
        self.assertFalse(diagnostics["stale_document_invalidated"])

    def test_not_modified_clears_stale_failure_diagnostics(self) -> None:
        conn = RecordingWriteConnection()
        candidate = CrawlCandidate(
            source_id="source-1",
            canonical_url="https://example.com/detail/1",
            host="example.com",
            etag="etag",
            http_last_modified=None,
            priority_score=5,
        )
        response = HttpPayload(
            final_url="https://example.com/detail/1",
            status_code=304,
            content_type="text/html",
            headers={},
            body=b"",
        )

        store_not_modified(conn, candidate, response)

        sql, _ = conn.recording_cursor.executions[0]
        self.assertIn("metadata", sql)
        self.assertIn("last_failure_diagnostics", sql)
        self.assertEqual(conn.commit_count, 1)


class RecordingValidationCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))


class RecordingValidationConnection:
    def __init__(self) -> None:
        self.recording_cursor = RecordingValidationCursor()

    def cursor(self, **kwargs):
        del kwargs
        return self.recording_cursor


class ValidationPersistenceTests(unittest.TestCase):
    def test_verdict_is_bound_to_the_exact_document_version(self) -> None:
        conn = RecordingValidationConnection()
        page = extract_page(
            "<html><body><article><h1>청년 취업 지원</h1><p>"
            + ("청년센터 면접수당 신청 안내입니다. " * 15)
            + "</p></article></body></html>"
        )
        policy = PolicyEvidence(
            policy_no="P-1",
            policy_name="청년 취업 지원",
            organization_names=("청년센터",),
            distinctive_terms=("면접수당",),
        )

        counts = store_link_validations(
            conn,
            source_id="source-1",
            document_id="document-7",
            final_url="https://example.org/policy/P-1",
            http_status=200,
            extracted=page,
            policies=(policy,),
        )

        sql, params = conn.recording_cursor.executions[0]
        self.assertEqual(counts, {"VERIFIED": 1})
        self.assertIn("validated_document_id = %s", sql)
        self.assertEqual(params[6], "document-7")

    def test_iframe_document_never_auto_verifies_without_manual_review(self) -> None:
        conn = RecordingValidationConnection()
        page = extract_page(
            "<html><body><article><h1>청년 취업 지원</h1><p>"
            + ("청년센터 면접수당 신청 안내입니다. " * 15)
            + "</p></article></body></html>"
        )
        page = replace(
            page,
            retrieval={
                "mode": "NAVER_POST_IFRAME",
                "depth": 1,
                "container_host": "blog.naver.com",
                "container_path": "/youthcenter/123456789012",
                "content_host": "blog.naver.com",
                "content_path": "/PostView.naver",
                "blog_id": "youthcenter",
                "log_no": "123456789012",
                "content_http_status": 200,
                "content_type": "text/html; charset=utf-8",
                "response_bytes": 1024,
            },
        )
        policy = PolicyEvidence(
            policy_no="P-1",
            policy_name="청년 취업 지원",
            organization_names=("청년센터",),
            distinctive_terms=("면접수당",),
        )

        counts = store_link_validations(
            conn,
            source_id="source-1",
            document_id="document-iframe",
            final_url="https://blog.naver.com/youthcenter/123456789012",
            http_status=200,
            extracted=page,
            policies=(policy,),
        )

        _, params = conn.recording_cursor.executions[0]
        self.assertEqual(counts, {"REVIEW_REQUIRED": 1})
        self.assertEqual(params[0], "REVIEW_REQUIRED")
        self.assertEqual(params[6], "document-iframe")


if __name__ == "__main__":
    unittest.main()
