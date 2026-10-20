"""urllib 抓取：条件 GET、限速、重试、重定向逐跳校验、robots.txt。"""

from __future__ import annotations

import gzip
import random
import socket
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
    urlopen as _stdlib_urlopen,
)
from urllib.robotparser import RobotFileParser

from .config import NewsConfig
from .sources import host_allowed

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    def __init__(self, message: str, *, url: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class RobotsDisallowed(FetchError):
    pass


@dataclass(frozen=True)
class FetchResponse:
    url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    not_modified: bool = False


class _RedirectHandler(HTTPRedirectHandler):
    def __init__(self, domains: Sequence[str], max_redirects: int) -> None:
        super().__init__()
        self.domains = tuple(domains)
        self.max_redirects = max_redirects
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > self.max_redirects:
            raise HTTPError(newurl, code, "too many redirects", headers, fp)
        if self.domains and not host_allowed(newurl, self.domains):
            raise HTTPError(newurl, code, "redirect host not allowed", headers, fp)
        old_scheme = urlsplit(req.full_url).scheme.lower()
        new_scheme = urlsplit(newurl).scheme.lower()
        if old_scheme == "https" and new_scheme == "http":
            raise HTTPError(newurl, code, "https->http downgrade blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _backoff(attempt: int) -> float:
    return min(2.0 ** attempt + random.uniform(0, 0.5), 8.0)


def _decode_body(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    if body[:2] == b"\x1f\x8b" or "gzip" in encoding:
        try:
            return gzip.decompress(body)
        except OSError as exc:
            raise FetchError("gzip decompress failed") from exc
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            try:
                return zlib.decompress(body, -zlib.MAX_WBITS)
            except zlib.error as exc:
                raise FetchError("deflate decompress failed") from exc
    return body


class Fetcher:
    def __init__(
        self,
        cfg: NewsConfig,
        *,
        opener=None,
        urlopen: Callable | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._sleeper = sleeper
        self._clock = clock
        self._urlopen = urlopen
        self._opener = opener
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, bool] = {}

    # ------------------------------------------------------------------
    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
        accept: str = "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        domains: Sequence[str] | None = None,
        check_robots: bool = True,
    ) -> FetchResponse:
        max_bytes = max_bytes or self.cfg.feed_max_bytes
        if domains is not None and not host_allowed(url, domains):
            raise FetchError(f"URL not allowed: {url}", url=url)
        if self.cfg.respect_robots and check_robots:
            if not self._robots_allowed(url, domains):
                raise RobotsDisallowed(f"robots.txt disallows: {url}", url=url)

        host = (urlsplit(url).hostname or "").lower()
        last_error: Exception | None = None
        for attempt in range(self.cfg.http_retries + 1):
            self._throttle(host)
            headers = {
                "User-Agent": self.cfg.user_agent,
                "Accept": accept,
                "Accept-Encoding": "identity",
            }
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            request = Request(url, headers=headers, method="GET")
            try:
                response = self._open(request, self.cfg.http_timeout_s, domains)
            except HTTPError as exc:
                if exc.code == 304:
                    return FetchResponse(
                        url=url,
                        final_url=url,
                        status=304,
                        headers=dict(exc.headers.items()) if exc.headers else {},
                        body=b"",
                        not_modified=True,
                    )
                last_error = exc
                if exc.code in _RETRYABLE_STATUS and attempt < self.cfg.http_retries:
                    delay = _backoff(attempt)
                    if exc.code == 429 and exc.headers is not None:
                        retry_after = exc.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            delay = min(float(retry_after), 120.0)
                    self._sleeper(delay)
                    continue
                raise FetchError(
                    f"HTTP {exc.code} for {url}", url=url, status=exc.code
                ) from exc
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt < self.cfg.http_retries:
                    self._sleeper(_backoff(attempt))
                    continue
                raise FetchError(f"network error for {url}: {exc}", url=url) from exc

            try:
                status = int(getattr(response, "status", None) or response.getcode() or 0)
                final_url = response.geturl() or url
                if domains is not None and not host_allowed(final_url, domains):
                    raise FetchError(
                        f"redirected outside allowed domains: {final_url}", url=final_url
                    )
                response_headers = dict(response.headers.items()) if response.headers else {}
                content_length = response_headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            raise FetchError(
                                f"response too large ({content_length} bytes)", url=url
                            )
                    except ValueError:
                        pass
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise FetchError(f"response exceeds {max_bytes} bytes", url=url)
                body = _decode_body(
                    body, response_headers.get("Content-Encoding", "")
                )
                if len(body) > max_bytes:
                    raise FetchError(f"decompressed response exceeds {max_bytes} bytes", url=url)
                return FetchResponse(
                    url=url,
                    final_url=final_url,
                    status=status,
                    headers=response_headers,
                    body=body,
                )
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        raise FetchError(f"fetch failed for {url}: {last_error}", url=url)

    # ------------------------------------------------------------------
    def _open(self, request: Request, timeout: float, domains: Sequence[str] | None):
        if self._urlopen is not None:
            return self._urlopen(request, timeout=timeout)
        opener = self._opener
        if opener is None:
            # 每个请求单独建 opener，保证重定向计数和域名白名单都是本请求的。
            opener = build_opener(
                ProxyHandler(),
                _RedirectHandler(domains or (), self.cfg.max_redirects),
            )
        return opener.open(request, timeout=timeout)

    def _throttle(self, host: str) -> None:
        if not host or self.cfg.host_min_interval_s <= 0:
            return
        now = self._clock()
        last = self._last_request.get(host)
        if last is not None:
            wait = self.cfg.host_min_interval_s - (now - last)
            if wait > 0:
                self._sleeper(wait)
        self._last_request[host] = self._clock()

    def _robots_allowed(self, url: str, domains: Sequence[str] | None) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        key = f"{parsed.scheme}://{host}"
        if key in self._robots:
            return self._robots[key]
        robots_url = f"{key}/robots.txt"
        allowed = True
        try:
            response = self.fetch(
                robots_url,
                max_bytes=512 * 1024,
                accept="text/plain, */*",
                domains=domains,
                check_robots=False,
            )
            if response.status in (401, 403):
                allowed = False
            elif response.body.strip():
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.body.decode("utf-8", "replace").splitlines())
                allowed = parser.can_fetch(self.cfg.user_agent, url)
        except FetchError as exc:
            if exc.status in (401, 403):
                allowed = False
            # 404/410/网络失败：按允许处理
        self._robots[key] = allowed
        return allowed
