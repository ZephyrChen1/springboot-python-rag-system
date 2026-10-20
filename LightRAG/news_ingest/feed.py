"""RSS 2.0 / RSS 1.0(RDF) / Atom 解析，只依赖标准库。"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

from .sources import Source


class FeedParseError(ValueError):
    pass


@dataclass(frozen=True)
class FeedItem:
    source_id: str
    title: str
    link: str
    guid: str | None
    published_at: str | None
    updated_at: str | None
    summary_html: str
    content_html: str
    feed_url: str


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _direct_children(element: ET.Element, names: set[str]) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) in names]


def _first_child(element: ET.Element, names: set[str]) -> ET.Element | None:
    for child in list(element):
        if _local(child.tag) in names:
            return child
    return None


def _inner_html(element: ET.Element) -> str:
    if len(element):
        try:
            return ET.tostring(element, encoding="unicode", method="html")
        except (TypeError, ValueError):
            return "".join(element.itertext())
    return element.text or ""


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _clean_title(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _link_from_rss(item: ET.Element, feed_url: str) -> str:
    link_el = _first_child(item, {"link"})
    link = _text(link_el)
    if not link and link_el is not None:
        # RSS 1.0/RDF 常见写法：<link rdf:resource="https://..."/>
        for attr, value in link_el.attrib.items():
            if attr.lower().endswith("resource") or attr.lower() == "href":
                link = value.strip()
                break
    if link:
        return urljoin(feed_url, link)
    guid_el = _first_child(item, {"guid", "id"})
    guid = _text(guid_el)
    if guid and (guid.startswith("http://") or guid.startswith("https://")):
        return guid
    return ""


def _link_from_atom(entry: ET.Element, feed_url: str) -> str:
    fallback = ""
    for child in list(entry):
        if _local(child.tag) != "link":
            continue
        rel = (child.attrib.get("rel") or "").lower()
        href = (child.attrib.get("href") or "").strip()
        if href:
            if rel in ("", "alternate"):
                return urljoin(feed_url, href)
            if not fallback:
                fallback = urljoin(feed_url, href)
        text = (child.text or "").strip()
        if text and not fallback:
            fallback = urljoin(feed_url, text)
    if fallback:
        return fallback
    id_el = _first_child(entry, {"id"})
    guid = _text(id_el)
    if guid and (guid.startswith("http://") or guid.startswith("https://")):
        return guid
    return ""


def _rss_items(root: ET.Element) -> list[ET.Element]:
    # RSS 2.0: rss/channel/item；RDF: rdf/item；有些源直接用 item
    items = [el for el in root.iter() if _local(el.tag) == "item"]
    return items


def _atom_entries(root: ET.Element) -> list[ET.Element]:
    return [el for el in root.iter() if _local(el.tag) == "entry"]


def parse_feed(data: bytes, *, source: Source, feed_url: str) -> list[FeedItem]:
    if not isinstance(data, (bytes, bytearray)) or not data.strip():
        raise FeedParseError("empty feed")
    lowered = bytes(data).lower()
    if b"<!entity" in lowered:
        raise FeedParseError("XML entity declarations are not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedParseError(f"invalid XML: {exc}") from exc

    root_name = _local(root.tag)
    if root_name in ("rss", "rdf"):
        entries = _rss_items(root)
    elif root_name == "feed":
        entries = _atom_entries(root)
    else:
        raise FeedParseError(f"unsupported feed root: {root_name or root.tag!r}")

    items: list[FeedItem] = []
    for entry in entries:
        if root_name in ("rss", "rdf"):
            title_el = _first_child(entry, {"title"})
            title = _clean_title(_text(title_el))
            link = _link_from_rss(entry, feed_url)
            guid_el = _first_child(entry, {"guid", "id"})
            guid = _text(guid_el) or None
            published = parse_date(
                _text(_first_child(entry, {"pubdate", "published", "date"}))
            )
            updated = parse_date(_text(_first_child(entry, {"updated", "lastbuilddate"})))
            content_el = _first_child(entry, {"encoded", "content"})
            summary_el = _first_child(entry, {"description", "summary"})
            content_html = _inner_html(content_el) if content_el is not None else ""
            summary_html = _inner_html(summary_el) if summary_el is not None else ""
        else:
            title_el = _first_child(entry, {"title"})
            title = _clean_title(_text(title_el))
            link = _link_from_atom(entry, feed_url)
            guid_el = _first_child(entry, {"id"})
            guid = _text(guid_el) or None
            published = parse_date(_text(_first_child(entry, {"published", "issued", "date"})))
            updated = parse_date(_text(_first_child(entry, {"updated", "modified"})))
            content_el = _first_child(entry, {"content"})
            summary_el = _first_child(entry, {"summary"})
            content_html = _inner_html(content_el) if content_el is not None else ""
            summary_html = _inner_html(summary_el) if summary_el is not None else ""

        if not title or not link:
            continue
        if not summary_html:
            summary_html = content_html
        if not content_html:
            content_html = summary_html
        items.append(
            FeedItem(
                source_id=source.id,
                title=title,
                link=link,
                guid=guid,
                published_at=published or updated,
                updated_at=updated,
                summary_html=summary_html,
                content_html=content_html,
                feed_url=feed_url,
            )
        )
    return items
