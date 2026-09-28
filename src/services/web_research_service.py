"""AgentCore Platform v1.0 — CMN-C2-258 web research service (lawful collection).

Collects public competitor facts from the web. `shared.tools.web_search` in
SDK 1.0.0 is a placeholder (returns a fixed string), so real collection is
implemented here as an injectable template-local service (docs/02_design.md
Design Decision; request:coe candidate for a platform-standard safe_fetch).

Lawful-collection manners implemented here (著作権法47条の5 — 情報解析の範囲):
  - robots.txt compliance (Disallow paths are never fetched; fail-closed
    only on explicit disallow — unreachable robots.txt defaults to allow)
  - per-domain rate limit (default 1 req/sec, injectable clock/sleep)
  - timeout + per-competitor page cap
  - SSRF guard: http(s) only; internal/loopback/link-local/metadata IPs blocked
    (competitor pages are arbitrary PUBLIC hosts — no allowlist, guard always on)
  - explicit User-Agent; source URL + fetched-at recorded per page
  - fact extraction only: title / meta description / heading-level snippets,
    each clipped to `fact_max_chars` — full page bodies are never stored
  - login-required / paid content out of scope (no cookies, no auth)

Network egress is injected (`fetcher`) so unit tests and CI stay offline.
"""

from __future__ import annotations

import html as _html
import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import urlparse

from framework.errors import SecurityViolationError

_ALLOWED_SCHEMES = ("http", "https")
_MAX_BYTES = 2_000_000  # 2 MB response cap
_USER_AGENT = "CMN-C2-258-CompetitiveAnalysis/0.1"


def _is_blocked_ip(host: str) -> bool:
    """Resolve host and block private / loopback / link-local / metadata IPs."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # unresolvable — fail closed
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip_str == "169.254.169.254"  # cloud metadata endpoint
        ):
            return True
    return False


def assert_url_allowed(url: str) -> None:
    """Raise SecurityViolationError unless the URL is a safe public target."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SecurityViolationError(
            f"web_research: scheme '{parsed.scheme}' not permitted (http/https only)"
        )
    host = parsed.hostname or ""
    if not host or _is_blocked_ip(host):
        raise SecurityViolationError(
            f"web_research: host '{host}' resolves to a blocked (internal/metadata) address"
        )


def _default_fetch(url: str, timeout: float) -> tuple[int, str]:
    """Default network egress via urllib — REDIRECTS ARE NOT FOLLOWED.

    Fetcher contract: returns (status, body); for a 3xx response the body is
    the Location header value. Redirect following is done by the SERVICE loop
    so every hop is re-validated (SSRF guard + robots + rate limit) — an
    upstream 302 to an internal/metadata address must never be fetched.
    """
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
            return None  # surface the 3xx instead of following it

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 — scheme/IP guarded above
            body = resp.read(_MAX_BYTES + 1)
            return resp.status, body[:_MAX_BYTES].decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in _REDIRECT_CODES:
            return exc.code, exc.headers.get("Location", "") or ""
        return exc.code, ""


_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_REDIRECT_HOPS = 3


_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]*content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_HEADING_RE = re.compile(r"<h[123][^>]*>(.*?)</h[123]>", re.IGNORECASE | re.DOTALL)


def extract_facts(body: str, *, fact_max_chars: int = 90, max_facts: int = 12) -> list[str]:
    """Extract short factual snippets — never the full page body.

    Each snippet is clipped to `fact_max_chars` so the S-3 long-quote gate
    (verbatim-reproduction guard) holds by construction.
    """
    facts: list[str] = []

    def _clean(fragment: str) -> str:
        text = _TAG_RE.sub(" ", fragment)
        text = _html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    m = _TITLE_RE.search(body)
    if m and _clean(m.group(1)):
        facts.append(_clean(m.group(1))[:fact_max_chars])
    m = _META_DESC_RE.search(body)
    if m and _clean(m.group(1)):
        facts.append(_clean(m.group(1))[:fact_max_chars])
    for hm in _HEADING_RE.finditer(body):
        text = _clean(hm.group(1))
        if text:
            facts.append(text[:fact_max_chars])
        if len(facts) >= max_facts:
            break
    # dedupe, preserve order
    seen: set[str] = set()
    return [f for f in facts if not (f in seen or seen.add(f))][:max_facts]


class WebResearchService:
    """Injectable public-web fact collector (robots + rate limit + SSRF guard)."""

    def __init__(
        self,
        fetcher: Callable[[str, float], tuple[int, str]] | None = None,
        *,
        rate_limit_sec: float = 1.0,
        timeout_sec: float = 10.0,
        max_pages_per_competitor: int = 3,
        fact_max_chars: int = 90,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        now_iso: Callable[[], str] | None = None,
    ) -> None:
        import datetime as _dt
        import time as _time

        self._fetch = fetcher or _default_fetch
        self._rate_limit = rate_limit_sec
        self._timeout = timeout_sec
        self._max_pages = max_pages_per_competitor
        self._fact_max_chars = fact_max_chars
        self._sleep = sleep or _time.sleep
        self._clock = clock or _time.monotonic
        self._now_iso = now_iso or (lambda: _dt.datetime.now(_dt.timezone.utc).isoformat())
        self._last_hit: dict[str, float] = {}
        self._robots_cache: dict[str, str | None] = {}

    # -- robots.txt -----------------------------------------------------
    _ROBOTS_BLOCKED = "__blocked__"  # sentinel: robots unresolvable via safe hops

    def _fetch_robots_body(self, origin: str) -> str | None:
        """Fetch robots.txt following redirects with the SAME hop guards.

        Fetchers do not follow redirects (contract: 3xx returns Location), so
        the Location value must never be parsed as a robots body. Each hop is
        SSRF-revalidated; RFC 9309: unavailable (4xx/unreachable) => allow
        (None), but a redirect chain we cannot safely resolve => fail closed.
        """
        current = f"{origin}/robots.txt"
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            try:
                assert_url_allowed(current)
                status, body = self._fetch(current, self._timeout)
            except SecurityViolationError:
                return self._ROBOTS_BLOCKED  # redirect into blocked space — fail closed
            except Exception:  # noqa: BLE001 — unreachable robots.txt => allow
                return None
            if status in _REDIRECT_CODES:
                location = (body or "").strip()
                if not location:
                    return None
                from urllib.parse import urljoin

                current = urljoin(current, location)
                continue
            return body if status < 400 else None
        return self._ROBOTS_BLOCKED  # too many redirects — fail closed

    def _robots_allows(self, url: str) -> bool:
        from urllib.robotparser import RobotFileParser

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            self._robots_cache[origin] = self._fetch_robots_body(origin)
        body = self._robots_cache[origin]
        if body is None:
            return True
        if body == self._ROBOTS_BLOCKED:
            return False
        rp = RobotFileParser()
        rp.parse(body.splitlines())
        return rp.can_fetch(_USER_AGENT, url)

    # -- rate limit -----------------------------------------------------
    def _respect_rate_limit(self, host: str) -> None:
        last = self._last_hit.get(host)
        now = self._clock()
        if last is not None and (now - last) < self._rate_limit:
            self._sleep(self._rate_limit - (now - last))
        self._last_hit[host] = self._clock()

    # -- public API -------------------------------------------------------
    def research(self, competitor: str, urls: list[str]) -> list[dict]:
        """Collect facts for one competitor from the given public URLs.

        Partial failures are tolerated and reported per-page (`error` key);
        zero successful pages is a valid outcome (the pipeline continues with
        internal win/loss data only).
        """
        results: list[dict] = []
        for url in urls[: self._max_pages]:
            entry: dict = {"competitor": competitor, "url": url, "fetched_at": self._now_iso()}
            try:
                status, body = self._fetch_with_guarded_redirects(url)
                if status is None:
                    entry.update(facts=[], error=body)  # body carries the guard reason
                elif status >= 400:
                    entry.update(facts=[], error=f"HTTP {status}")
                else:
                    entry["facts"] = extract_facts(
                        body[:_MAX_BYTES], fact_max_chars=self._fact_max_chars
                    )
            except SecurityViolationError as exc:
                entry.update(facts=[], error=str(exc))
            except Exception as exc:  # noqa: BLE001 — per-page failure tolerated
                entry.update(facts=[], error=f"fetch failed: {exc}")
            results.append(entry)
        return results

    def _fetch_with_guarded_redirects(self, url: str) -> tuple[int | None, str]:
        """Fetch with EVERY redirect hop re-validated (SSRF + robots + rate limit).

        Fetchers never follow redirects themselves (contract: 3xx returns the
        Location value as body). Returns (status, body); (None, reason) when a
        guard stopped the chain. Raises SecurityViolationError from the SSRF
        guard on the initial URL (caller reports it per-page).
        """
        current = url
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            assert_url_allowed(current)
            if not self._robots_allows(current):
                return None, "disallowed by robots.txt"
            self._respect_rate_limit(urlparse(current).hostname or "")
            status, body = self._fetch(current, self._timeout)
            if status not in _REDIRECT_CODES:
                return status, body
            location = (body or "").strip()
            if not location:
                return status, ""
            from urllib.parse import urljoin

            current = urljoin(current, location)
        return None, f"too many redirects (> {_MAX_REDIRECT_HOPS})"
