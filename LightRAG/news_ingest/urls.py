"""URL 归一化与 KB 文件名生成。"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING_KEYS = {
    "gclid",
    "fbclid",
    "yclid",
    "msclkid",
    "igshid",
    "_hsenc",
    "_hsmi",
    "ref",
    "source",
    "mc_cid",
    "mc_eid",
}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_BAD_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key in _TRACKING_KEYS or key.startswith("utm_")


def _public_host(host: str) -> bool:
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


def canonicalize_url(url: str, *, base_url: str | None = None) -> str:
    """返回稳定的 canonical URL；非法输入返回空串。"""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url:
        return ""
    if base_url:
        url = urljoin(base_url, url)
    if len(url) > 2048:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or not _public_host(host):
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parsed.path or "/"
    # 尾斜杠归一化；根路径保留 "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking(key)
    ]
    query_pairs.sort(key=lambda pair: (pair[0], pair[1]))
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def sha256_short(value: str, length: int = 8) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _slugify(value: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:max_len].strip("-")


def build_filename(
    source_id: str,
    published_at: str | None,
    title: str,
    canonical_url: str,
    *,
    max_len: int = 120,
) -> str:
    """生成稳定、安全、ASCII 的 KB 文件名。"""
    source = _BAD_FILENAME_CHARS.sub("-", (source_id or "source").lower()).strip("._-")
    source = source or "source"
    date = "unknown"
    if published_at:
        try:
            date = datetime.fromisoformat(published_at.replace("Z", "+00:00")).strftime("%Y%m%d")
        except ValueError:
            date = re.sub(r"[^0-9]", "", published_at)[:8] or "unknown"
    if date == "unknown":
        date = datetime.now(timezone.utc).strftime("%Y%m%d")

    slug = _slugify(title or "", 40) or "release"
    digest = sha256_short(canonical_url or title or "unknown", 8)
    suffix = f"-{digest}.txt"
    prefix = f"ai-news-{source}-{date}-"
    # 保证总长 <= max_len
    available = max_len - len(prefix) - len(suffix)
    if available < 1:
        # 极端长 source id：截断 source
        source = source[: max(1, max_len - len(date) - len(suffix) - len("ai-news--"))]
        prefix = f"ai-news-{source}-{date}-"
        available = max(1, max_len - len(prefix) - len(suffix))
    slug = slug[:available].strip("-") or "release"
    filename = f"{prefix}{slug}{suffix}"
    filename = _BAD_FILENAME_CHARS.sub("-", filename)
    filename = filename.strip("._- ")
    if not filename.lower().endswith(".txt"):
        filename = (filename[: max_len - 4] + ".txt") if len(filename) > max_len else filename + ".txt"
    if len(filename) > max_len:
        filename = filename[: max_len - 4].rstrip("._- ") + ".txt"
    if filename.startswith(".") or ".." in filename:
        filename = "ai-news-" + filename.lstrip(".")
    return filename


def safe_filename(name: str, *, max_len: int = 120) -> str:
    """通用文件名白名单清洗，供测试/边界场景复用。"""
    cleaned = _BAD_FILENAME_CHARS.sub("-", name or "").strip("._- ")
    cleaned = cleaned.replace("..", "-")
    if len(cleaned) > max_len:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 5:
            cleaned = stem[: max_len - len(ext) - 1].rstrip("._- ") + "." + ext
        else:
            cleaned = cleaned[:max_len].rstrip("._- ")
    return cleaned or "article.txt"
