"""sitemap.xml / sitemapindex.xml 解析（一层 index）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urljoin


class SitemapParseError(ValueError):
    pass


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: str | None


@dataclass(frozen=True)
class SitemapDocument:
    entries: tuple[SitemapEntry, ...]
    children: tuple[str, ...]


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            # 有些 sitemap 只有日期
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def parse_sitemap(
    data: bytes,
    *,
    include_prefixes: Sequence[str] = (),
    since: datetime | None = None,
    base_url: str | None = None,
) -> SitemapDocument:
    if not isinstance(data, (bytes, bytearray)) or not data.strip():
        raise SitemapParseError("empty sitemap")
    if b"<!entity" in bytes(data).lower():
        raise SitemapParseError("XML entity declarations are not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SitemapParseError(f"invalid XML: {exc}") from exc
    root_name = _local(root.tag)

    if root_name == "sitemapindex":
        children: list[str] = []
        for node in _children(root, "sitemap"):
            loc = _text(_children(node, "loc")[0] if _children(node, "loc") else None)
            if loc:
                children.append(urljoin(base_url, loc) if base_url else loc)
        return SitemapDocument(entries=(), children=tuple(children))

    if root_name != "urlset":
        raise SitemapParseError(f"unsupported sitemap root: {root_name or root.tag!r}")

    entries: list[SitemapEntry] = []
    for node in _children(root, "url"):
        loc_el = _children(node, "loc")
        loc = _text(loc_el[0]) if loc_el else ""
        if not loc:
            continue
        if base_url:
            loc = urljoin(base_url, loc)
        if include_prefixes and not any(loc.startswith(prefix) for prefix in include_prefixes):
            continue
        lastmod_el = _children(node, "lastmod")
        lastmod_raw = _text(lastmod_el[0]) if lastmod_el else ""
        if since is not None:
            dt = _parse_dt(lastmod_raw)
            if dt is None or dt < since:
                continue
        entries.append(SitemapEntry(loc=loc, lastmod=lastmod_raw or None))

    def sort_key(entry: SitemapEntry) -> tuple[int, float]:
        dt = _parse_dt(entry.lastmod)
        return (0, -dt.timestamp()) if dt else (1, 0.0)

    entries.sort(key=sort_key)
    return SitemapDocument(entries=tuple(entries), children=())
