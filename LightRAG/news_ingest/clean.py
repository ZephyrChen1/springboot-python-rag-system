"""用标准库 HTMLParser 抽取正文，不引入 bs4/lxml。

清洗目标是"给知识库和 LLM 看的纯文本"，不是像素级还原网页：
  - 丢掉脚本、导航、页脚、表单、分享/订阅等 boilerplate；
  - 优先 <article>/<main>，多个时取最长的；
  - 保留段落换行、列表符号、表格单元格分隔。
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from html.parser import HTMLParser

_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "form",
    "nav",
    "header",
    "footer",
    "aside",
    "button",
    "input",
    "select",
    "textarea",
    "template",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "blockquote",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "tr",
    "br",
}
_BOILER_RE = re.compile(
    r"(?:^|[\s_-])(?:cookie|consent|newsletter|subscribe|share|social|related|"
    r"comments?|promo|banner|breadcrumb|sidebar)(?:$|[\s_-])",
    re.IGNORECASE,
)
_LINE_BOILER_RE = re.compile(
    r"(?i)^(?:advertisement|share(?: this)?(?: article| page)?|subscribe(?: to .*)?|"
    r"sign up for .*|follow us.*|cookie(?:s)? .*|we use cookies.*|accept all cookies|"
    r"related articles?|read more|back to top|©.*)$"
)
_LOW_QUALITY_RE = re.compile(
    r"(?i)(just a moment|enable javascript|cf-chl|checking your browser|access denied|"
    r"attention required|please verify you are a human)"
)


class CleanError(ValueError):
    pass


@dataclass(frozen=True)
class CleanResult:
    text: str
    title: str
    truncated: bool
    low_quality: bool = False


def _is_boilerplate(attrs: list[tuple[str, str | None]]) -> bool:
    for key, value in attrs:
        if key in ("class", "id", "role", "aria-label") and value:
            if _BOILER_RE.search(value.lower()):
                return True
    return False


def _normalize(raw: str) -> str:
    text = _html.unescape(raw or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines: list[str] = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", line).strip()
        if not line:
            lines.append("")
            continue
        if len(line) < 300 and _LINE_BOILER_RE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    window_start = max(0, max_chars - 400)
    candidates = [
        cut.rfind("\n\n"),
        cut.rfind(". "),
        cut.rfind("。"),
        cut.rfind("! "),
        cut.rfind("? "),
        cut.rfind("\n"),
    ]
    boundary = max(candidates)
    if boundary >= window_start:
        cut = cut[: boundary + 1] if cut[boundary] not in "\n" else cut[:boundary]
    return cut.rstrip(), True


class _Extractor(HTMLParser):
    def __init__(self, *, prefer_article: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.prefer_article = prefer_article
        self.all_parts: list[str] = []
        self.article_stack: list[list[str]] = []
        self.article_texts: list[str] = []
        self.open_tags: list[tuple[str, bool]] = []
        self.skip_depth = 0
        self.title_parts: list[str] = []
        self.og_title = ""
        self.in_title = False
        self.in_pre = False

    # -- helpers -----------------------------------------------------
    def _append(self, text: str) -> None:
        if not text:
            return
        self.all_parts.append(text)
        if self.article_stack:
            self.article_stack[-1].append(text)

    def _newline(self) -> None:
        if self.all_parts and not self.all_parts[-1].endswith("\n"):
            self.all_parts.append("\n")
        if self.article_stack and (
            not self.article_stack[-1] or not self.article_stack[-1][-1].endswith("\n")
        ):
            self.article_stack[-1].append("\n")

    def _line_has_text(self) -> bool:
        for chunk in reversed(self.all_parts):
            if "\n" in chunk:
                return bool(chunk.rsplit("\n", 1)[-1].strip())
            if chunk.strip():
                return True
        return False

    # -- HTMLParser hooks -------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "meta":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            if attr_map.get("property", "").lower() == "og:title" and attr_map.get("content"):
                self.og_title = attr_map["content"]
            return
        if tag in _VOID_TAGS:
            if tag == "br" and self.skip_depth == 0:
                self._newline()
            return

        is_skip = tag in _SKIP_TAGS or _is_boilerplate(attrs)
        mark = False
        if self.skip_depth > 0:
            pass
        elif is_skip:
            self.skip_depth = 1
            mark = True
        self.open_tags.append((tag, mark))

        if self.skip_depth > 0:
            return
        if tag == "title":
            self.in_title = True
            return
        if tag in ("article", "main"):
            self.article_stack.append([])
        if tag in _BLOCK_TAGS:
            self._newline()
        if tag == "li":
            self._append("- ")
        elif tag in ("td", "th"):
            if self._line_has_text():
                self._append(" | ")
        if tag in ("pre", "code"):
            self.in_pre = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br" and self.skip_depth == 0:
            self._newline()
        elif tag == "meta":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        if tag == "title":
            self.in_title = False
        if self.skip_depth == 0 and tag in _BLOCK_TAGS:
            self._newline()
        if tag in ("article", "main") and self.skip_depth == 0 and self.article_stack:
            text = _normalize("".join(self.article_stack.pop()))
            if text:
                self.article_texts.append(text)
        if tag in ("pre", "code"):
            self.in_pre = False

        # 容忍不闭合/错配标签：找到最近的同名开标签，弹掉它及之上的标签
        for idx in range(len(self.open_tags) - 1, -1, -1):
            open_tag, was_skip = self.open_tags[idx]
            if open_tag == tag:
                removed = self.open_tags[idx:]
                del self.open_tags[idx:]
                if any(mark for _, mark in removed):
                    self.skip_depth = 0
                break

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth > 0:
            return
        self._append(data)

    def result(self) -> tuple[str, str, bool]:
        all_text = _normalize("".join(self.all_parts))
        title = _clean_title(self.og_title) or _clean_title("".join(self.title_parts))
        if not all_text:
            return "", title, True
        low_quality = bool(_LOW_QUALITY_RE.search(all_text[:2000]))
        best_article = ""
        if self.prefer_article and self.article_texts:
            best_article = max(self.article_texts, key=len)
        if best_article and len(best_article) >= max(200, int(len(all_text) * 0.4)):
            return best_article, title, low_quality
        return all_text, title, low_quality


def _clean_title(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_html(html: str, *, prefer_article: bool) -> tuple[str, str, bool]:
    if not isinstance(html, str) or not html.strip():
        return "", "", True
    parser = _Extractor(prefer_article=prefer_article)
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # HTMLParser 对极端损坏输入可能抛异常
        raise CleanError(f"HTML parse failed: {exc}") from exc
    return parser.result()


def extract_main_text(html: str, *, max_chars: int) -> CleanResult:
    text, title, low_quality = _parse_html(html, prefer_article=True)
    text, truncated = _truncate(text, max_chars)
    return CleanResult(
        text=text,
        title=title,
        truncated=truncated,
        low_quality=low_quality or not bool(text),
    )


def html_fragment_to_text(fragment: str, *, max_chars: int = 20000) -> str:
    """清洗 feed 里的 content:encoded / summary 片段；不强制 <article>。"""
    if not fragment:
        return ""
    text, _, _ = _parse_html(fragment, prefer_article=False)
    text, _ = _truncate(text, max_chars)
    return text


def is_low_quality_text(text: str) -> bool:
    return not bool((text or "").strip()) or bool(_LOW_QUALITY_RE.search(text[:2000]))
