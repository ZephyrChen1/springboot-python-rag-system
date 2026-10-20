"""SQLite 状态库：文章状态机、feed 游标、运行记录。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_url TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  guid TEXT,
  title TEXT NOT NULL,
  published_at TEXT,
  fetched_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  content_hash TEXT,
  feed_summary TEXT,
  status TEXT NOT NULL CHECK (status IN (
    'new','classified','filtered_out','failed',
    'ingest_pending','ingested','skipped')),
  category TEXT,
  importance INTEGER,
  title_zh TEXT,
  summary_zh TEXT,
  key_facts_json TEXT,
  doc_text TEXT,
  doc_filename TEXT,
  job_id TEXT,
  ingest_result TEXT,
  reason TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_due
  ON articles(status, next_retry_at, published_at);
CREATE INDEX IF NOT EXISTS idx_articles_source
  ON articles(source_id, published_at);
CREATE INDEX IF NOT EXISTS idx_articles_status_published
  ON articles(status, published_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_source_guid
  ON articles(source_id, guid) WHERE guid IS NOT NULL AND guid <> '';

CREATE TABLE IF NOT EXISTS feeds (
  source_id TEXT PRIMARY KEY,
  feed_url TEXT NOT NULL,
  etag TEXT,
  last_modified TEXT,
  last_fetched_at TEXT,
  last_status INTEGER,
  last_error TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,
  summary_json TEXT,
  exit_code INTEGER
);

CREATE TABLE IF NOT EXISTS run_sources (
  run_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  fetched INTEGER NOT NULL DEFAULT 0,
  candidates INTEGER NOT NULL DEFAULT 0,
  filtered INTEGER NOT NULL DEFAULT 0,
  ingested INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  PRIMARY KEY (run_id, source_id)
);
"""

_ALLOWED_COLUMNS = {
    "category",
    "importance",
    "title_zh",
    "summary_zh",
    "key_facts_json",
    "doc_text",
    "doc_filename",
    "job_id",
    "ingest_result",
    "reason",
    "retry_count",
    "next_retry_at",
    "last_error",
    "content_hash",
    "title",
    "published_at",
    "feed_summary",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ArticleRecord:
    canonical_url: str
    source_id: str
    guid: str | None
    title: str
    published_at: str | None
    feed_summary: str = ""
    content_hash: str | None = None


class StateStore:
    def __init__(self, path: Path | str, *, dry_run: bool = False) -> None:
        self.path = Path(path)
        self.dry_run = dry_run
        if dry_run:
            self._conn = sqlite3.connect(":memory:")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        if not dry_run:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    def upsert_article(self, record: ArticleRecord, *, now: str | None = None) -> tuple[int, bool]:
        now = now or now_iso()
        row = self._conn.execute(
            "SELECT id FROM articles WHERE canonical_url = ?", (record.canonical_url,)
        ).fetchone()
        if row is not None:
            self._conn.execute(
                """
                UPDATE articles SET last_seen_at=?, title=?, published_at=COALESCE(?, published_at),
                    feed_summary=CASE WHEN ? <> '' THEN ? ELSE feed_summary END,
                    guid=COALESCE(guid, ?), updated_at=?
                WHERE id=?
                """,
                (
                    now,
                    record.title,
                    record.published_at,
                    record.feed_summary,
                    record.feed_summary,
                    record.guid,
                    now,
                    row["id"],
                ),
            )
            self._conn.commit()
            return int(row["id"]), False
        if record.guid:
            row = self._conn.execute(
                "SELECT id FROM articles WHERE source_id=? AND guid=?",
                (record.source_id, record.guid),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE articles SET last_seen_at=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
                self._conn.commit()
                return int(row["id"]), False
        cursor = self._conn.execute(
            """
            INSERT INTO articles (
                canonical_url, source_id, guid, title, published_at,
                fetched_at, last_seen_at, content_hash, feed_summary,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                record.canonical_url,
                record.source_id,
                record.guid,
                record.title,
                record.published_at,
                now,
                now,
                record.content_hash,
                record.feed_summary,
                now,
                now,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid), True

    def get(self, canonical_url: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM articles WHERE canonical_url=?", (canonical_url,)
        ).fetchone()

    def set_status(self, canonical_url: str, status: str, *, now: str | None = None, **fields) -> bool:
        unknown = set(fields) - _ALLOWED_COLUMNS
        if unknown:
            raise ValueError(f"unsupported article columns: {sorted(unknown)}")
        now = now or now_iso()
        assignments = ["status=?", "updated_at=?"]
        values: list[object] = [status, now]
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(value)
        values.append(canonical_url)
        cursor = self._conn.execute(
            f"UPDATE articles SET {', '.join(assignments)} WHERE canonical_url=?",
            values,
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_failed(
        self,
        canonical_url: str,
        error: str,
        *,
        retry_max: int,
        retry_base_s: float,
        retry_max_s: float,
        permanent: bool = False,
        now: str | None = None,
    ) -> None:
        now = now or now_iso()
        row = self._conn.execute(
            "SELECT retry_count FROM articles WHERE canonical_url=?", (canonical_url,)
        ).fetchone()
        retry_count = int(row["retry_count"]) if row is not None else 0
        retry_count += 1
        next_retry: str | None = None
        if not permanent and retry_count < retry_max:
            current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            delay = min(retry_base_s * (2 ** (retry_count - 1)), retry_max_s)
            next_retry = (current + timedelta(seconds=delay)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.set_status(
            canonical_url,
            "failed",
            now=now,
            retry_count=retry_count,
            next_retry_at=next_retry,
            last_error=(error or "")[:1000],
        )

    def due_rows(
        self,
        *,
        now: str | None = None,
        limit: int | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> list[sqlite3.Row]:
        now = now or now_iso()
        clauses = [
            "(status IN ('new','classified') OR "
            "(status='failed' AND next_retry_at IS NOT NULL AND next_retry_at <= ?))"
        ]
        params: list[object] = [now]
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            clauses.append(f"source_id IN ({placeholders})")
            params.extend(source_ids)
        sql = (
            "SELECT * FROM articles WHERE "
            + " AND ".join(clauses)
            + " ORDER BY COALESCE(published_at,'') DESC, id ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return list(self._conn.execute(sql, params).fetchall())

    def pending_rows(
        self,
        *,
        now: str | None = None,
        limit: int | None = None,
        source_ids: Sequence[str] | None = None,
    ) -> list[sqlite3.Row]:
        now = now or now_iso()
        clauses = [
            "status='ingest_pending'",
            "(next_retry_at IS NULL OR next_retry_at <= ?)",
        ]
        params: list[object] = [now]
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            clauses.append(f"source_id IN ({placeholders})")
            params.extend(source_ids)
        sql = "SELECT * FROM articles WHERE " + " AND ".join(clauses) + " ORDER BY updated_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return list(self._conn.execute(sql, params).fetchall())

    def expire_unprocessed(self, cutoff_iso: str, *, now: str | None = None) -> int:
        """把超过日期窗口仍未处理的文章标记为 filtered_out('too_old')。

        只改 status='new'：已分类/已入库/待重试的行不受影响，也不删除任何行
        （canonical_url 是去重键，删掉会被下一次抓取重新插入）。
        时间基准是 published_at，缺失时回退 fetched_at。
        """
        now = now or now_iso()
        cursor = self._conn.execute(
            """
            UPDATE articles
               SET status='filtered_out', reason='too_old', updated_at=?
             WHERE status='new'
               AND COALESCE(NULLIF(published_at, ''), fetched_at) < ?
            """,
            (now, cutoff_iso),
        )
        self._conn.commit()
        return cursor.rowcount

    def reset_terminal(self, source_ids: Sequence[str] | None = None) -> int:
        clauses = ["status IN ('ingested','skipped','filtered_out','failed')"]
        params: list[object] = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            clauses.append(f"source_id IN ({placeholders})")
            params.extend(source_ids)
        cursor = self._conn.execute(
            """
            UPDATE articles SET
              status='new', retry_count=0, next_retry_at=NULL, last_error=NULL,
              category=NULL, importance=NULL, title_zh=NULL, summary_zh=NULL,
              key_facts_json=NULL, doc_text=NULL, doc_filename=NULL, job_id=NULL,
              ingest_result=NULL, reason=NULL, content_hash=NULL, updated_at=?
            WHERE %s
            """
            % " AND ".join(clauses),
            [now_iso(), *params],
        )
        self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    def get_feed_state(self, source_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM feeds WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            return {
                "source_id": source_id,
                "feed_url": "",
                "etag": None,
                "last_modified": None,
                "last_fetched_at": None,
                "last_status": None,
                "last_error": None,
                "consecutive_failures": 0,
            }
        return dict(row)

    def save_feed_state(
        self,
        source_id: str,
        feed_url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        status: int | None = None,
        error: str | None = None,
        truncated: bool = False,
        now: str | None = None,
    ) -> None:
        now = now or now_iso()
        existing = self._conn.execute(
            "SELECT consecutive_failures FROM feeds WHERE source_id=?", (source_id,)
        ).fetchone()
        failures = int(existing["consecutive_failures"]) if existing else 0
        if error:
            failures += 1
        else:
            failures = 0
        # 失败时保留旧 ETag；截断时 pipeline 传入 etag=None，必须清空，
        # 否则下一轮会带着旧 ETag 得到 304，永远拉不到更早的条目。
        keep_etag = bool(error)
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO feeds (source_id, feed_url, etag, last_modified, last_fetched_at,
                                   last_status, last_error, consecutive_failures)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    feed_url,
                    None if keep_etag else etag,
                    None if keep_etag else last_modified,
                    now,
                    status,
                    (error or "")[:1000] or ("feed truncated; etag cleared" if truncated else None),
                    failures,
                ),
            )
        elif keep_etag:
            self._conn.execute(
                """
                UPDATE feeds SET feed_url=?, last_fetched_at=?, last_status=?, last_error=?,
                                 consecutive_failures=?
                WHERE source_id=?
                """,
                (feed_url, now, status, (error or "")[:1000] or None, failures, source_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE feeds SET feed_url=?, etag=?, last_modified=?, last_fetched_at=?,
                                 last_status=?, last_error=?, consecutive_failures=?
                WHERE source_id=?
                """,
                (
                    feed_url,
                    etag,
                    last_modified,
                    now,
                    status,
                    (error or "")[:1000] or ("feed truncated; etag cleared" if truncated else None),
                    failures,
                    source_id,
                ),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    def record_run_start(self, run_id: str, mode: str, *, started_at: str | None = None) -> None:
        if self.dry_run:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, mode) VALUES (?, ?, ?)",
            (run_id, started_at or now_iso(), mode),
        )
        self._conn.commit()

    def record_run_source(
        self,
        run_id: str,
        source_id: str,
        *,
        fetched: int = 0,
        candidates: int = 0,
        filtered: int = 0,
        ingested: int = 0,
        skipped: int = 0,
        failed: int = 0,
        error: str | None = None,
    ) -> None:
        if self.dry_run:
            return
        self._conn.execute(
            """
            INSERT INTO run_sources (run_id, source_id, fetched, candidates, filtered,
                                     ingested, skipped, failed, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, source_id) DO UPDATE SET
              fetched=excluded.fetched, candidates=excluded.candidates,
              filtered=excluded.filtered, ingested=excluded.ingested,
              skipped=excluded.skipped, failed=excluded.failed, error=excluded.error
            """,
            (run_id, source_id, fetched, candidates, filtered, ingested, skipped, failed,
             (error or "")[:1000] or None),
        )
        self._conn.commit()

    def record_run_end(
        self,
        run_id: str,
        *,
        summary_json: str,
        exit_code: int,
        finished_at: str | None = None,
    ) -> None:
        if self.dry_run:
            return
        self._conn.execute(
            "UPDATE runs SET finished_at=?, summary_json=?, exit_code=? WHERE run_id=?",
            (finished_at or now_iso(), summary_json, exit_code, run_id),
        )
        self._conn.commit()

    def all_articles(self, *, source_id: str | None = None) -> list[sqlite3.Row]:
        if source_id:
            return list(
                self._conn.execute("SELECT * FROM articles WHERE source_id=? ORDER BY id", (source_id,))
            )
        return list(self._conn.execute("SELECT * FROM articles ORDER BY id"))
