"""编排：抓取 -> 清洗 -> 过滤 -> 入库 -> 状态记录。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from .classify import (
    ClassificationError,
    classify,
    is_ingestable,
    prefilter,
)
from .clean import CleanError, extract_main_text, html_fragment_to_text
from .config import NewsConfig
from .document import build_document
from .feed import FeedItem, parse_date, parse_feed
from .fetch import FetchError, FetchResponse, Fetcher, RobotsDisallowed
from .kb_client import (
    KBAuthError,
    KBBusy,
    KBClient,
    KBError,
    KBJobExpired,
    KBJobTimeout,
    KBPermanentError,
    KBTransientError,
)
from .llm import complete
from .sitemap import SitemapEntry, parse_sitemap
from .sources import (
    Source,
    SourceConfigError,
    host_allowed,
    load_sources,
    validate_url_for_source,
)
from .state import ArticleRecord, StateStore, now_iso
from .urls import canonicalize_url

logger = logging.getLogger("news_ingest")

TERMINAL = {"ingested", "skipped", "filtered_out"}


class PipelineFatal(RuntimeError):
    pass


class ContentUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RunOptions:
    dry_run: bool = False
    dry_run_no_llm: bool = False
    force: bool = False
    source_ids: tuple[str, ...] = ()
    limit: int | None = None
    since_days: int | None = None


@dataclass
class RunSummary:
    run_id: str
    mode: str
    started_at: str
    finished_at: str = ""
    fetched: int = 0
    candidates: int = 0
    filtered: int = 0
    expired: int = 0
    ingested: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0
    would_ingest: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    fatal_error: str = ""
    exit_code: int = 0
    elapsed_s: float = 0.0
    sources: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "fetched": self.fetched,
            "candidates": self.candidates,
            "filtered": self.filtered,
            "expired": self.expired,
            "ingested": self.ingested,
            "skipped": self.skipped,
            "failed": self.failed,
            "pending": self.pending,
            "would_ingest": self.would_ingest,
            "sources_ok": self.sources_ok,
            "sources_failed": self.sources_failed,
            "fatal_error": self.fatal_error,
            "exit_code": self.exit_code,
            "elapsed_s": round(self.elapsed_s, 3),
            "sources": self.sources,
        }


@dataclass(frozen=True)
class BodyResult:
    text: str
    origin: str
    permanent_reject: bool = False
    reason: str = ""


class RunLog:
    def __init__(self, directory: Path, run_id: str) -> None:
        self.path = Path(directory) / f"{run_id}.jsonl"

    def write(self, event: str, **fields) -> None:
        payload = {"ts": now_iso(), "event": event, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _item_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _title_from_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"\.(?:html?|md|php)$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug[:1].upper() + slug[1:] if slug else url


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _parse_sitemap_with_children(
    fetcher: Fetcher,
    cfg: NewsConfig,
    source: Source,
    response: FetchResponse,
    *,
    since: datetime | None,
) -> list[FeedItem]:
    document = parse_sitemap(
        response.body,
        include_prefixes=source.include_prefixes,
        since=since,
        base_url=response.final_url,
    )
    entries: list[SitemapEntry] = list(document.entries)
    for child in document.children[:5]:
        try:
            child_response = fetcher.fetch(
                child,
                max_bytes=cfg.feed_max_bytes,
                accept="application/xml, text/xml, */*",
                domains=source.domains,
            )
            child_doc = parse_sitemap(
                child_response.body,
                include_prefixes=source.include_prefixes,
                since=since,
                base_url=child_response.final_url,
            )
            entries.extend(child_doc.entries)
        except (FetchError, Exception) as exc:
            logger.warning("sitemap child failed source=%s url=%s: %s", source.id, child, exc)
    # 去重并按 lastmod 倒序
    seen: set[str] = set()
    unique: list[SitemapEntry] = []
    for entry in entries:
        if entry.loc in seen:
            continue
        seen.add(entry.loc)
        unique.append(entry)
    unique.sort(
        key=lambda e: (_item_dt(e.lastmod) is None, -(_item_dt(e.lastmod) or datetime.min.replace(tzinfo=timezone.utc)).timestamp())
    )
    return [
        FeedItem(
            source_id=source.id,
            title=_title_from_url(entry.loc),
            link=entry.loc,
            guid=entry.loc,
            published_at=parse_date(entry.lastmod),
            updated_at=parse_date(entry.lastmod),
            summary_html="",
            content_html="",
            feed_url=response.final_url,
        )
        for entry in unique
    ]


def fetch_source_items(
    fetcher: Fetcher,
    cfg: NewsConfig,
    source: Source,
    *,
    since: datetime | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[list[FeedItem], FetchResponse]:
    response = fetcher.fetch(
        source.feed_url,
        etag=etag,
        last_modified=last_modified,
        max_bytes=cfg.feed_max_bytes,
        domains=source.domains,
    )
    if response.not_modified:
        return [], response
    if source.type == "sitemap":
        items = _parse_sitemap_with_children(fetcher, cfg, source, response, since=since)
    else:
        items = parse_feed(response.body, source=source, feed_url=response.final_url)
    return items, response


def check_sources(cfg: NewsConfig, *, fetcher: Fetcher | None = None) -> list[dict]:
    fetcher = fetcher or Fetcher(cfg)
    sources = load_sources(cfg.sources_file, include_disabled=True)
    results: list[dict] = []
    for source in sources:
        entry: dict = {
            "id": source.id,
            "name": source.name,
            "enabled": source.enabled,
            "type": source.type,
            "feed_url": source.feed_url,
        }
        try:
            items, response = fetch_source_items(fetcher, cfg, source, since=None)
            dates = [item.published_at for item in items if item.published_at]
            entry.update(
                ok=True,
                status=response.status,
                items=len(items),
                latest=max(dates) if dates else None,
            )
        except Exception as exc:
            entry.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        results.append(entry)
    return results


class NewsPipeline:
    def __init__(
        self,
        cfg: NewsConfig,
        store: StateStore,
        fetcher: Fetcher,
        kb: KBClient,
        *,
        llm_fn: Callable | None = None,
        sources: Sequence[Source] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.fetcher = fetcher
        self.kb = kb
        self.llm_fn = llm_fn or (lambda messages: complete(messages, cfg=cfg))
        self._sources = list(sources) if sources is not None else None
        self._sleeper = sleeper
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._source_map: dict[str, Source] = {}
        self._summary: RunSummary | None = None
        self._run_log: RunLog | None = None
        self._monotonic_start = 0.0

    # ------------------------------------------------------------------
    def run(self, opts: RunOptions) -> RunSummary:
        run_id = f"{self._now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        mode = "dry-run-no-llm" if opts.dry_run_no_llm else ("dry-run" if opts.dry_run else "run")
        summary = RunSummary(run_id=run_id, mode=mode, started_at=_iso(self._now()))
        self._summary = summary
        self._monotonic_start = time.monotonic()
        self._run_log = None if opts.dry_run else RunLog(self.cfg.run_log_dir, run_id)
        if not opts.dry_run:
            self.store.record_run_start(run_id, mode, started_at=summary.started_at)
            self._log("run_start", mode=mode, opts=self._opts_dict(opts))

        try:
            sources = self._select_sources(opts)
            if not sources:
                raise PipelineFatal("no sources selected")
            if not opts.dry_run and not opts.dry_run_no_llm:
                if not self.kb.health():
                    raise PipelineFatal(
                        f"API health check failed: {self.cfg.api_base_url}（请先启动单 worker API）"
                    )
            elif not opts.dry_run:
                if not self.kb.health():
                    raise PipelineFatal(
                        f"API health check failed: {self.cfg.api_base_url}（请先启动单 worker API）"
                    )

            selected_ids = tuple(source.id for source in sources)
            if opts.force and not opts.dry_run:
                reset = self.store.reset_terminal(selected_ids)
                self._log("force_reset", count=reset)

            self._resume_pending(opts, selected_ids)

            for source in sources:
                stats = self._stats(source.id)
                try:
                    items, response = fetch_source_items(
                        self.fetcher,
                        self.cfg,
                        source,
                        since=self._since(opts),
                        etag=self.store.get_feed_state(source.id).get("etag")
                        if not opts.dry_run
                        else None,
                        last_modified=self.store.get_feed_state(source.id).get("last_modified")
                        if not opts.dry_run
                        else None,
                    )
                    if response.not_modified:
                        stats["http_status"] = 304
                        summary.sources_ok += 1
                        self._log("source_not_modified", source=source.id, status=304)
                        self._record_source(run_id, source.id)
                        continue
                    stats["http_status"] = response.status
                    self._upsert_items(source, items, opts, stats)
                    summary.fetched += len(items)
                    summary.sources_ok += 1
                    if not opts.dry_run:
                        self._save_feed_state(source, response, items, opts)
                    self._log(
                        "source_fetched",
                        source=source.id,
                        status=response.status,
                        items=len(items),
                        stored=stats.get("stored", 0),
                    )
                except Exception as exc:
                    summary.sources_failed += 1
                    stats["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                    logger.warning("source failed source=%s: %s", source.id, exc)
                    self._log("source_failed", source=source.id, error=stats["error"])
                    if not opts.dry_run:
                        try:
                            self.store.save_feed_state(
                                source.id,
                                source.feed_url,
                                status=None,
                                error=stats["error"],
                            )
                        except Exception:
                            logger.exception("failed to save feed error state")
                finally:
                    self._record_source(run_id, source.id)

            # 7 天清理：把过期未处理的文章标记为 filtered_out(too_old)，不删行。
            # --force 时跳过，否则刚被 reset 成 new 的旧文章会被立刻重新过滤掉。
            if not opts.dry_run and not opts.force:
                cutoff_dt = self._since(opts)
                if cutoff_dt is not None:
                    cutoff = _iso(cutoff_dt)
                    summary.expired = self.store.expire_unprocessed(cutoff)
                    self._log("expired", count=summary.expired, cutoff=cutoff)

            due = self._select_due_rows(opts, selected_ids)
            summary.candidates = len(due)
            for idx, row in enumerate(due):
                if self._timed_out():
                    remaining = len(due) - idx
                    summary.pending += max(0, remaining)
                    self._log("run_timeout", remaining=remaining)
                    break
                try:
                    self._process_row(row, opts)
                except (KBAuthError, PipelineFatal):
                    raise
                except Exception as exc:
                    logger.exception("article failed url=%s", row["canonical_url"])
                    self._fail(row, f"{type(exc).__name__}: {exc}")

            if not opts.dry_run:
                summary.pending = len(
                    self.store.pending_rows(now=now_iso(), source_ids=selected_ids)
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            summary.fatal_error = f"{type(exc).__name__}: {exc}"[:2000]
            logger.error("run fatal: %s", exc)
            self._log("run_fatal", error=summary.fatal_error)
        finally:
            summary.finished_at = _iso(self._now())
            summary.elapsed_s = time.monotonic() - self._monotonic_start
            summary.exit_code = self._exit_code(summary)
            for source_id in list(summary.sources):
                self._record_source(run_id, source_id)
            if not opts.dry_run:
                try:
                    self.store.record_run_end(
                        run_id,
                        summary_json=json.dumps(summary.to_dict(), ensure_ascii=False),
                        exit_code=summary.exit_code,
                    )
                except Exception:
                    logger.exception("failed to persist run end")
            self._log("run_end", **summary.to_dict())
        return summary

    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        value = self._now_fn()
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def _window_days(self, opts: RunOptions) -> int:
        return self.cfg.since_days if opts.since_days is None else opts.since_days

    def _since(self, opts: RunOptions) -> datetime | None:
        days = self._window_days(opts)
        return None if days <= 0 else self._now() - timedelta(days=days)

    def _log(self, event: str, **fields) -> None:
        if self._run_log is not None:
            try:
                fields.setdefault("run_id", self._summary.run_id if self._summary else "")
                self._run_log.write(event, **fields)
            except Exception:
                logger.exception("failed to write run log")

    @staticmethod
    def _opts_dict(opts: RunOptions) -> dict:
        return {
            "dry_run": opts.dry_run,
            "dry_run_no_llm": opts.dry_run_no_llm,
            "force": opts.force,
            "source_ids": list(opts.source_ids),
            "limit": opts.limit,
            "since_days": opts.since_days,
        }

    def _select_sources(self, opts: RunOptions) -> list[Source]:
        if self._sources is None:
            all_sources = load_sources(self.cfg.sources_file, include_disabled=True)
        else:
            all_sources = list(self._sources)
        selected: list[Source] = []
        if opts.source_ids:
            wanted = set(opts.source_ids)
            found = {s.id for s in all_sources}
            missing = sorted(wanted - found)
            if missing:
                raise PipelineFatal(f"unknown source(s): {', '.join(missing)}")
            selected = [s for s in all_sources if s.id in wanted]
            for source in selected:
                if not source.enabled:
                    logger.warning("source %s is disabled in sources.json but explicitly selected", source.id)
        else:
            selected = [s for s in all_sources if s.enabled]
        self._source_map = {s.id: s for s in selected}
        return selected

    def _stats(self, source_id: str) -> dict:
        if self._summary is None:
            raise RuntimeError("summary not initialized")
        return self._summary.sources.setdefault(
            source_id,
            {
                "fetched": 0,
                "candidates": 0,
                "filtered": 0,
                "ingested": 0,
                "skipped": 0,
                "failed": 0,
                "error": None,
                "stored": 0,
                "http_status": None,
            },
        )

    def _record_source(self, run_id: str, source_id: str) -> None:
        if self.store.dry_run:
            return
        stats = self._stats(source_id)
        try:
            self.store.record_run_source(
                run_id,
                source_id,
                fetched=int(stats.get("fetched", 0)),
                candidates=int(stats.get("candidates", 0)),
                filtered=int(stats.get("filtered", 0)),
                ingested=int(stats.get("ingested", 0)),
                skipped=int(stats.get("skipped", 0)),
                failed=int(stats.get("failed", 0)),
                error=stats.get("error"),
            )
        except Exception:
            logger.exception("failed to record run source")

    def _exit_code(self, summary: RunSummary) -> int:
        if summary.fatal_error:
            return 1
        if summary.sources_failed or summary.failed:
            return 2
        return 0

    def _timed_out(self) -> bool:
        return (time.monotonic() - self._monotonic_start) > self.cfg.run_max_minutes * 60

    # ------------------------------------------------------------------
    def _upsert_items(
        self, source: Source, items: Sequence[FeedItem], opts: RunOptions, stats: dict
    ) -> None:
        since = self._since(opts)
        max_items = min(source.max_items, self.cfg.max_feed_items)
        eligible: list[FeedItem] = []
        for item in items:
            if since is not None:
                dt = _item_dt(item.published_at)
                if dt is not None and dt < since:
                    continue
            eligible.append(item)
        to_store = eligible[:max_items]
        truncated = len(eligible) > max_items
        stats["feed_truncated"] = truncated
        stats["fetched"] = len(eligible)
        created = 0
        for item in to_store:
            canonical = canonicalize_url(item.link, base_url=source.site_url or item.feed_url)
            if not canonical or not validate_url_for_source(canonical, source):
                self._log(
                    "drop_invalid_url",
                    source=source.id,
                    url=item.link,
                    title=item.title[:200],
                )
                continue
            feed_text = html_fragment_to_text(
                item.content_html or item.summary_html,
                max_chars=self.cfg.doc_body_max_chars,
            )
            _, is_new = self.store.upsert_article(
                ArticleRecord(
                    canonical_url=canonical,
                    source_id=source.id,
                    guid=item.guid,
                    title=item.title,
                    published_at=item.published_at,
                    feed_summary=feed_text,
                )
            )
            if is_new:
                created += 1
        stats["stored"] = stats.get("stored", 0) + created

    def _save_feed_state(
        self, source: Source, response: FetchResponse, items: Sequence[FeedItem], opts: RunOptions
    ) -> None:
        stats = self._stats(source.id)
        truncated = bool(stats.get("feed_truncated"))
        if truncated:
            self.store.save_feed_state(
                source.id,
                source.feed_url,
                etag=None,
                last_modified=None,
                status=response.status,
                truncated=True,
            )
            return
        self.store.save_feed_state(
            source.id,
            source.feed_url,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            status=response.status,
        )

    # ------------------------------------------------------------------
    def _select_due_rows(self, opts: RunOptions, source_ids: Sequence[str]):
        rows = self.store.due_rows(now=now_iso(), source_ids=source_ids)
        # 0 = 不限（env 与 --limit 同一套语义）
        limit = self.cfg.max_articles_per_run if opts.limit is None else max(0, opts.limit)
        per_source_cap = self.cfg.max_per_source_per_run
        buckets: dict[str, deque] = {}
        for row in rows:
            buckets.setdefault(row["source_id"], deque()).append(row)
        selected: list = []
        per_source: dict[str, int] = {}
        exhausted = False
        # 即使不限量也按源轮转：它决定 run_max_minutes 超时时的处理顺序，
        # 避免单个高产源占满前排、其他源一直排不上。
        while not exhausted and any(buckets.values()):
            progressed = False
            for source_id in sorted(list(buckets)):
                bucket = buckets[source_id]
                if not bucket:
                    continue
                if per_source_cap > 0 and per_source.get(source_id, 0) >= per_source_cap:
                    continue
                selected.append(bucket.popleft())
                per_source[source_id] = per_source.get(source_id, 0) + 1
                progressed = True
                if limit > 0 and len(selected) >= limit:
                    exhausted = True
                    break
            if not progressed:
                break
        for row in selected:
            self._stats(row["source_id"])["candidates"] = (
                int(self._stats(row["source_id"]).get("candidates", 0)) + 1
            )
        return selected

    # ------------------------------------------------------------------
    def _process_row(self, row, opts: RunOptions) -> None:
        source = self._source_map.get(row["source_id"])
        if source is None:
            raise PipelineFatal(f"source not selected: {row['source_id']}")
        canonical = row["canonical_url"]
        status = row["status"]
        if status in TERMINAL:
            return
        if status == "classified" or (status == "failed" and row["doc_text"]):
            if not row["doc_text"]:
                self._fail(row, "missing rendered document", permanent=True)
                return
            if opts.dry_run:
                self._log("preview_upload", url=canonical, filename=row["doc_filename"])
                return
            self._upload_doc(row, source)
            return

        item = FeedItem(
            source_id=source.id,
            title=row["title"],
            link=canonical,
            guid=row["guid"],
            published_at=row["published_at"],
            updated_at=row["published_at"],
            summary_html=row["feed_summary"] or "",
            content_html="",
            feed_url=source.feed_url,
        )
        pre = prefilter(item, since_days=self._window_days(opts), now=self._now())
        if not pre.passed:
            self._filtered(row, pre.reason)
            return

        try:
            body_result = self._acquire_body(source, item, row)
        except ContentUnavailable as exc:
            self._fail(row, f"content_unavailable: {exc}")
            return
        if body_result.permanent_reject:
            self._filtered(row, body_result.reason)
            return
        body = body_result.text
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        if row["content_hash"] and row["content_hash"] != content_hash:
            if not self.cfg.reingest_on_change:
                self.store.set_status(
                    canonical,
                    "skipped",
                    content_hash=content_hash,
                    reason="content_changed_ignored",
                )
                self._bump(row["source_id"], "skipped")
                self._summary.skipped += 1
                self._log("content_changed_ignored", url=canonical)
                return

        if opts.dry_run_no_llm:
            from .urls import build_filename

            filename = build_filename(source.id, item.published_at, item.title, canonical)
            self._log(
                "preview_no_llm",
                source=source.id,
                url=canonical,
                title=item.title[:200],
                filename=filename,
                body_chars=len(body),
                origin=body_result.origin,
            )
            self._summary.would_ingest += 1
            return

        try:
            classification = classify(item, body, llm_fn=self.llm_fn, cfg=self.cfg)
        except ClassificationError as exc:
            self._fail(row, f"classification: {exc}")
            return
        if not is_ingestable(classification, self.cfg.importance_threshold):
            self.store.set_status(
                canonical,
                "filtered_out",
                category=classification.category,
                importance=classification.importance,
                title_zh=classification.title_zh or None,
                summary_zh=classification.summary_zh or None,
                key_facts_json=json.dumps(list(classification.key_facts), ensure_ascii=False),
                reason=classification.reason or "not_ingestable",
                content_hash=content_hash,
            )
            self._summary.filtered += 1
            self._bump(row["source_id"], "filtered")
            self._log(
                "classification_rejected",
                url=canonical,
                category=classification.category,
                importance=classification.importance,
                reason=classification.reason[:300],
            )
            return

        document = build_document(source, item, canonical, body, classification, self.cfg)
        self.store.set_status(
            canonical,
            "classified",
            category=classification.category,
            importance=classification.importance,
            title_zh=classification.title_zh,
            summary_zh=classification.summary_zh,
            key_facts_json=json.dumps(list(classification.key_facts), ensure_ascii=False),
            doc_text=document.text,
            doc_filename=document.filename,
            content_hash=content_hash,
            reason=classification.reason,
        )
        self._log(
            "classified",
            url=canonical,
            category=classification.category,
            importance=classification.importance,
            filename=document.filename,
        )
        if opts.dry_run:
            self._summary.would_ingest += 1
            logger.info(
                "[dry-run] would ingest %s (%s, importance=%s)",
                document.filename,
                classification.category,
                classification.importance,
            )
            self._log(
                "preview_ingest",
                url=canonical,
                filename=document.filename,
                text=document.text[:1000],
            )
            return
        updated = self.store.get(canonical)
        if updated is None:
            self._fail(row, "article disappeared after classification", permanent=True)
            return
        self._upload_doc(updated, source)

    # ------------------------------------------------------------------
    def _acquire_body(self, source: Source, item: FeedItem, row) -> BodyResult:
        feed_text = row["feed_summary"] or html_fragment_to_text(item.summary_html, max_chars=self.cfg.doc_body_max_chars)
        if len(feed_text) >= self.cfg.feed_full_content_min_chars:
            return BodyResult(feed_text, "feed")
        if not self.cfg.fetch_article:
            if len(feed_text) >= self.cfg.min_body_chars:
                return BodyResult(feed_text, "feed")
            return BodyResult("", "none", True, "insufficient_content")

        try:
            response = self.fetcher.fetch(
                item.link,
                max_bytes=self.cfg.article_max_bytes,
                accept="text/html, application/xhtml+xml, */*",
                domains=source.domains,
            )
        except RobotsDisallowed as exc:
            if len(feed_text) >= self.cfg.min_body_chars:
                return BodyResult(feed_text, "feed")
            raise ContentUnavailable(f"robots_disallowed: {exc}") from exc
        except FetchError as exc:
            if len(feed_text) >= self.cfg.min_body_chars:
                return BodyResult(feed_text, "feed")
            raise ContentUnavailable(str(exc)) from exc

        content_type = (response.headers.get("Content-Type") or "").lower()
        body_bytes = response.body
        if content_type and "html" not in content_type and "xml" not in content_type:
            if len(feed_text) >= self.cfg.min_body_chars:
                return BodyResult(feed_text, "feed")
            raise ContentUnavailable(f"non_html: {content_type[:100]}")
        if not content_type and not body_bytes.lstrip().startswith(b"<"):
            if len(feed_text) >= self.cfg.min_body_chars:
                return BodyResult(feed_text, "feed")
            raise ContentUnavailable("non_html_body")
        try:
            html_text = body_bytes.decode(_charset(content_type), errors="replace")
        except LookupError:
            html_text = body_bytes.decode("utf-8", errors="replace")
        try:
            clean = extract_main_text(html_text, max_chars=self.cfg.doc_body_max_chars)
        except CleanError as exc:
            if len(feed_text) >= self.cfg.min_body_chars:
                return BodyResult(feed_text, "feed")
            raise ContentUnavailable(f"clean_failed: {exc}") from exc
        if clean.low_quality:
            if len(feed_text) >= self.cfg.min_body_chars:
                return BodyResult(feed_text, "feed")
            raise ContentUnavailable("low_quality_page")
        if len(clean.text) >= self.cfg.min_body_chars:
            return BodyResult(clean.text, "article")
        if len(feed_text) >= self.cfg.min_body_chars:
            return BodyResult(feed_text, "feed")
        return BodyResult("", "none", True, "insufficient_content")

    # ------------------------------------------------------------------
    def _upload_doc(self, row, source: Source) -> None:
        canonical = row["canonical_url"]
        doc_text = row["doc_text"]
        if not doc_text:
            self._fail(row, "missing doc_text", permanent=True)
            return
        filename = row["doc_filename"] or ""
        if not filename:
            from .urls import build_filename

            filename = build_filename(source.id, row["published_at"], row["title"], canonical)
        self._log("upload_start", url=canonical, filename=filename, chars=len(doc_text))
        try:
            submit = getattr(self.kb, "submit_waiting_for_lock", None) or self.kb.submit
            job_id = submit(filename, doc_text.encode("utf-8"))
        except KBAuthError:
            raise
        except KBPermanentError as exc:
            self._fail(row, f"upload_permanent: {exc}", permanent=True)
            return
        except (KBBusy, KBTransientError, KBError) as exc:
            self._fail(row, f"upload: {type(exc).__name__}: {exc}")
            return
        self.store.set_status(canonical, "ingest_pending", job_id=job_id, next_retry_at=None)
        self._log("upload_accepted", url=canonical, job_id=job_id)
        try:
            job = self.kb.wait(job_id)
        except KBJobTimeout as exc:
            next_retry = _iso(self._now() + timedelta(seconds=max(self.cfg.poll_interval_s, 30.0)))
            self.store.set_status(canonical, "ingest_pending", job_id=job_id, next_retry_at=next_retry)
            self._summary.pending += 1
            self._log("upload_pending", url=canonical, job_id=job_id, error=str(exc))
            return
        except KBAuthError:
            raise
        except KBJobExpired as exc:
            self._fail(row, f"job_expired: {exc}")
            return
        except (KBBusy, KBTransientError, KBError) as exc:
            self._fail(row, f"job_poll: {type(exc).__name__}: {exc}")
            return
        self._finalize_job(row, job)

    def _finalize_job(self, row, job) -> None:
        canonical = row["canonical_url"]
        status = job.status
        result = job.result[:1000]
        if status == "ingested":
            self.store.set_status(
                canonical,
                "ingested",
                ingest_result=result,
                job_id=job.job_id,
                next_retry_at=None,
                last_error=None,
            )
            self._summary.ingested += 1
            self._bump(row["source_id"], "ingested")
            self._log("ingested", url=canonical, job_id=job.job_id, result=result[:300])
        elif status == "skipped":
            self.store.set_status(
                canonical,
                "skipped",
                ingest_result=result,
                job_id=job.job_id,
                next_retry_at=None,
                last_error=None,
            )
            self._summary.skipped += 1
            self._bump(row["source_id"], "skipped")
            self._log("skipped", url=canonical, job_id=job.job_id, result=result[:300])
        else:
            self._fail(row, f"ingest result: {result or 'empty/unknown'}")

    def _resume_pending(self, opts: RunOptions, source_ids: Sequence[str]) -> None:
        if opts.dry_run or opts.dry_run_no_llm:
            return
        rows = self.store.pending_rows(now=now_iso(), source_ids=source_ids)
        for row in rows:
            source = self._source_map.get(row["source_id"])
            if source is None:
                self._fail(row, "source not selected", permanent=True)
                continue
            job_id = row["job_id"]
            if job_id:
                try:
                    job = self.kb.get_job(job_id)
                except KBAuthError:
                    raise
                except KBError as exc:
                    self._fail(row, f"resume_poll: {type(exc).__name__}: {exc}")
                    continue
                if job is None:
                    self._fail(row, "resume: job expired (404); will re-upload", permanent=False)
                    continue
                if job.get("done"):
                    result = str(job.get("result") or "")
                    from .kb_client import JobResult, classify_result

                    self._finalize_job(
                        row,
                        JobResult(
                            job_id=job_id,
                            status=classify_result(result),
                            result=result,
                            stage=str(job.get("stage") or ""),
                        ),
                    )
                    continue
                updated = _item_dt(row["updated_at"])
                if updated and (self._now() - updated).total_seconds() > self.cfg.job_timeout_s:
                    self._fail(row, "resume: job timeout", permanent=False)
                    continue
                self._summary.pending += 1
                self._log("resume_running", url=row["canonical_url"], job_id=job_id)
                continue
            if row["doc_text"]:
                self._upload_doc(row, source)
            else:
                self._fail(row, "ingest_pending without doc_text", permanent=True)

    # ------------------------------------------------------------------
    def _filtered(self, row, reason: str) -> None:
        self.store.set_status(row["canonical_url"], "filtered_out", reason=reason[:500])
        self._summary.filtered += 1
        self._bump(row["source_id"], "filtered")
        logger.info("article filtered source=%s url=%s: %s", row["source_id"], row["canonical_url"], reason)
        self._log("filtered", url=row["canonical_url"], reason=reason[:300])

    def _fail(self, row, error: str, *, permanent: bool = False) -> None:
        logger.warning(
            "article failed source=%s url=%s: %s%s",
            row["source_id"],
            row["canonical_url"],
            error,
            " (permanent)" if permanent else "",
        )
        self.store.mark_failed(
            row["canonical_url"],
            error,
            retry_max=self.cfg.retry_max,
            retry_base_s=self.cfg.retry_base_s,
            retry_max_s=self.cfg.retry_max_s,
            permanent=permanent,
        )
        self._summary.failed += 1
        self._bump(row["source_id"], "failed")
        self._log("failed", url=row["canonical_url"], error=error[:1000], permanent=permanent)

    def _bump(self, source_id: str, key: str) -> None:
        stats = self._stats(source_id)
        stats[key] = int(stats.get(key, 0)) + 1
