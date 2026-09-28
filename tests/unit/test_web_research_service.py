# CMN-C2-258 — Unit Tests: web research service (lawful collection, offline)

from __future__ import annotations

import pytest

from framework.errors import SecurityViolationError
from src.services.web_research_service import (
    WebResearchService,
    assert_url_allowed,
    extract_facts,
)

_HTML = (
    "<html><head><title>CompX ERP Suite</title>"
    '<meta name="description" content="CompX の ERP は保守込み価格。">'
    "</head><body><h1>機能一覧</h1><h2>価格プラン</h2></body></html>"
)


def _svc(fetch_map=None, **over):
    calls: list[str] = []

    def fetcher(url: str, timeout: float):
        calls.append(url)
        if url.endswith("robots.txt"):
            return 200, (fetch_map or {}).get("robots", "User-agent: *\nAllow: /")
        entry = (fetch_map or {}).get(url)
        if entry is not None:
            return entry
        return 200, _HTML

    kwargs = {"fetcher": fetcher, "rate_limit_sec": 0.0}
    kwargs.update(over)
    svc = WebResearchService(**kwargs)
    return svc, calls


class TestSsrfGuard:
    def test_non_http_scheme_blocked(self):
        with pytest.raises(SecurityViolationError):
            assert_url_allowed("ftp://example.com/file")

    def test_loopback_blocked(self):
        with pytest.raises(SecurityViolationError):
            assert_url_allowed("http://127.0.0.1/admin")

    def test_metadata_ip_blocked(self):
        with pytest.raises(SecurityViolationError):
            assert_url_allowed("http://169.254.169.254/latest/meta-data")

    def test_blocked_url_reported_per_page_not_raised(self):
        svc, _ = _svc()
        results = svc.research("CompX", ["http://127.0.0.1/x"])
        assert results[0]["facts"] == []
        assert "blocked" in results[0]["error"]


@pytest.fixture()
def offline_dns(monkeypatch):
    """Skip real DNS resolution in the SSRF guard — CI runs fully offline.

    The guard itself is exercised in TestSsrfGuard with literal IPs (no DNS).
    """
    import src.services.web_research_service as wrs

    monkeypatch.setattr(wrs, "_is_blocked_ip", lambda host: False)


class TestRobots:
    def test_disallowed_path_not_fetched(self, offline_dns):
        svc, calls = _svc(fetch_map={"robots": "User-agent: *\nDisallow: /private"})
        results = svc.research("CompX", ["https://example.com/private/page"])
        assert results[0]["error"] == "disallowed by robots.txt"
        assert not any("/private/page" in u for u in calls)

    def test_unreachable_robots_defaults_to_allow(self, offline_dns):
        def fetcher(url, timeout):
            if url.endswith("robots.txt"):
                raise OSError("connection refused")
            return 200, _HTML

        svc = WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)
        results = svc.research("CompX", ["https://example.com/p"])
        assert results[0]["facts"]


class TestRateLimit:
    def test_sleeps_between_same_domain_hits(self, offline_dns):
        sleeps: list[float] = []
        clock_values = iter([0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35])
        svc, _ = _svc(
            rate_limit_sec=1.0,
            sleep=sleeps.append,
            clock=lambda: next(clock_values),
        )
        svc.research("CompX", ["https://example.com/a", "https://example.com/b"])
        assert sleeps and sleeps[0] > 0


class TestRedirectGuard:
    """F-01: every redirect hop is re-validated (SSRF + robots + rate limit)."""

    def test_redirect_to_blocked_target_is_stopped(self, offline_dns, monkeypatch):
        # The redirect TARGET resolves to a blocked address — even though DNS
        # for normal hosts is stubbed, the metadata IP literal stays blocked.
        import src.services.web_research_service as wrs

        monkeypatch.setattr(
            wrs, "_is_blocked_ip", lambda host: host == "169.254.169.254"
        )

        def fetcher(url, timeout):
            if url.endswith("robots.txt"):
                return 200, "User-agent: *\nAllow: /"
            if url == "https://example.com/start":
                return 302, "http://169.254.169.254/latest/meta-data"
            return 200, _HTML

        svc = WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)
        results = svc.research("CompX", ["https://example.com/start"])
        assert results[0]["facts"] == []
        assert "blocked" in results[0]["error"]

    def test_redirect_target_robots_rechecked(self, offline_dns):
        robots = "User-agent: *\nDisallow: /private"

        def fetcher(url, timeout):
            if url.endswith("robots.txt"):
                return 200, robots
            if url == "https://example.com/start":
                return 302, "https://example.com/private/page"
            return 200, _HTML

        svc = WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)
        results = svc.research("CompX", ["https://example.com/start"])
        assert results[0]["error"] == "disallowed by robots.txt"

    def test_allowed_redirect_followed(self, offline_dns):
        def fetcher(url, timeout):
            if url.endswith("robots.txt"):
                return 200, "User-agent: *\nAllow: /"
            if url == "http://example.com/start":
                return 301, "https://example.com/start"
            return 200, _HTML

        svc = WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)
        results = svc.research("CompX", ["http://example.com/start"])
        assert results[0]["facts"]

    def test_redirected_robots_txt_followed_not_parsed_as_body(self, offline_dns):
        # robots.txt itself returns 301 — the Location value must be followed
        # (with hop guards), never parsed as a permissive robots body.
        def fetcher(url, timeout):
            if url == "https://example.com/robots.txt":
                return 301, "https://www.example.com/robots.txt"
            if url == "https://www.example.com/robots.txt":
                return 200, "User-agent: *\nDisallow: /private"
            return 200, _HTML

        svc = WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)
        results = svc.research("CompX", ["https://example.com/private/page"])
        assert results[0]["error"] == "disallowed by robots.txt"

    def test_robots_redirect_loop_fails_closed(self, offline_dns):
        def fetcher(url, timeout):
            if url.endswith("robots.txt"):
                return 302, "https://example.com/robots.txt"
            return 200, _HTML

        svc = WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)
        results = svc.research("CompX", ["https://example.com/p"])
        assert results[0]["error"] == "disallowed by robots.txt"

    def test_redirect_loop_bounded(self, offline_dns):
        def fetcher(url, timeout):
            if url.endswith("robots.txt"):
                return 200, "User-agent: *\nAllow: /"
            return 302, "https://example.com/loop"

        svc = WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)
        results = svc.research("CompX", ["https://example.com/loop"])
        assert "too many redirects" in results[0]["error"]


class TestCollection:
    def test_extract_facts_clipped_and_deduped(self, offline_dns):
        long_text = "あ" * 300
        body = f"<html><head><title>{long_text}</title></head><body><h1>{long_text}</h1></body></html>"
        facts = extract_facts(body, fact_max_chars=90)
        assert facts
        assert all(len(f) <= 90 for f in facts)
        assert len(facts) == 1  # identical snippets deduped

    def test_page_cap_respected(self, offline_dns):
        svc, calls = _svc(max_pages_per_competitor=1)
        results = svc.research("CompX", ["https://example.com/a", "https://example.com/b"])
        assert len(results) == 1

    def test_http_error_tolerated_per_page(self, offline_dns):
        svc, _ = _svc(fetch_map={"https://example.com/404": (404, "")})
        results = svc.research(
            "CompX", ["https://example.com/404", "https://example.com/ok"]
        )
        assert results[0]["error"] == "HTTP 404"
        assert results[1]["facts"]

    def test_source_and_fetched_at_recorded(self, offline_dns):
        svc, _ = _svc(now_iso=lambda: "2026-07-03T00:00:00+00:00")
        results = svc.research("CompX", ["https://example.com/p"])
        assert results[0]["url"] == "https://example.com/p"
        assert results[0]["fetched_at"] == "2026-07-03T00:00:00+00:00"
