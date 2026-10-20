"""源注册表的加载与安全校验。

安全边界：
  - feed/文章/重定向后的 URL 必须落在该源声明的域名内；
  - 拒绝私有/环回/link-local IP（防 SSRF）；
  - 拒绝 userinfo（`https://user:pass@host/`）。
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

_ID_RE = re.compile(r"^[a-z0-9_-]+$")
_MAX_URL_LEN = 2048
_DEFAULT_PORTS = {"http": 80, "https": 443}


class SourceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    enabled: bool
    type: str
    feed_url: str
    site_url: str
    domains: tuple[str, ...]
    article_url_patterns: tuple[str, ...]
    include_prefixes: tuple[str, ...]
    language: str
    max_items: int
    note: str = ""

    def patterns_allow(self, url: str) -> bool:
        if not self.article_url_patterns:
            return True
        return any(re.match(pattern, url) is not None for pattern in self.article_url_patterns)


def _is_public_host(host: str) -> bool:
    """拒绝 IP 字面量中的私有/环回/link-local 地址。

    域名解析结果不在这里判断：DNS 可能变化，且本工具只访问
    sources.json 中人工审核过的官方域名。真正的 SSRF 边界是
    域名白名单 + 重定向逐跳校验。
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def host_allowed(url: str, domains: Sequence[str]) -> bool:
    if not isinstance(url, str) or len(url) > _MAX_URL_LEN:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or not _is_public_host(host):
        return False
    for domain in domains:
        d = domain.lower().rstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def validate_url_for_source(url: str, source: Source) -> bool:
    return host_allowed(url, source.domains) and source.patterns_allow(url)


def _require_str(data: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise SourceConfigError(f"source {data.get('id')!r}: missing/invalid {key}")
    return value.strip()


def _str_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise SourceConfigError(f"source {data.get('id')!r}: {key} must be a list of strings")
    return tuple(v.strip() for v in value)


def _source_from_dict(data: Any) -> Source:
    if not isinstance(data, dict):
        raise SourceConfigError("source entry must be an object")
    source_id = _require_str(data, "id")
    if not _ID_RE.match(source_id):
        raise SourceConfigError(f"invalid source id: {source_id!r}")
    source_type = _require_str(data, "type").lower()
    if source_type not in ("rss", "sitemap"):
        raise SourceConfigError(f"source {source_id!r}: type must be rss or sitemap")
    feed_url = _require_str(data, "feed_url")
    domains = _str_list(data, "domains")
    if not domains:
        raise SourceConfigError(f"source {source_id!r}: domains must not be empty")
    if not host_allowed(feed_url, domains):
        raise SourceConfigError(f"source {source_id!r}: feed_url is not inside domains")
    patterns = _str_list(data, "article_url_patterns")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SourceConfigError(
                f"source {source_id!r}: invalid article_url_pattern {pattern!r}: {exc}"
            ) from exc
    max_items = data.get("max_items", 50)
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
        raise SourceConfigError(f"source {source_id!r}: max_items must be a positive int")
    return Source(
        id=source_id,
        name=_require_str(data, "name"),
        enabled=bool(data.get("enabled", True)),
        type=source_type,
        feed_url=feed_url,
        site_url=data.get("site_url", "") or "",
        domains=domains,
        article_url_patterns=patterns,
        include_prefixes=_str_list(data, "include_prefixes"),
        language=data.get("language", "en") or "en",
        max_items=max_items,
        note=data.get("note", "") or "",
    )


def load_sources(path: Path, *, include_disabled: bool = False) -> list[Source]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceConfigError(f"sources file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceConfigError(f"sources file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise SourceConfigError("sources file must contain a 'sources' array")
    sources: list[Source] = []
    seen: set[str] = set()
    for entry in raw["sources"]:
        source = _source_from_dict(entry)
        if source.id in seen:
            raise SourceConfigError(f"duplicate source id: {source.id}")
        seen.add(source.id)
        if include_disabled or source.enabled:
            sources.append(source)
    return sources
