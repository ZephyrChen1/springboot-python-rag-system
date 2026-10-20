"""只读查询 crawler_state/news.db，供 Web 端“抓取动态”展示。

这个模块刻意不复用 news_ingest.state.StateStore：后者的构造函数会建目录、
切换 WAL、执行建表脚本，属于写操作。这里只打开已存在的数据库，并且用
SQLite URI 的 mode=ro + PRAGMA query_only 双重保证只读。
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from news_ingest.clean import html_fragment_to_text
from news_ingest.config import load_config

STATUSES = (
    "new",
    "classified",
    "filtered_out",
    "failed",
    "ingest_pending",
    "ingested",
    "skipped",
)

_SOURCE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

_LIST_COLUMNS = """
    id, source_id, title, title_zh, summary_zh, feed_summary, status,
    category, importance, published_at, fetched_at, doc_filename,
    canonical_url, reason, last_error
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _truncate(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _parse_key_facts(raw: Any) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()][:6]


def _parse_summary(raw: Any) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class NewsStore:
    """一个请求一个实例；连接不跨请求共享。"""

    def __init__(self, db_path: Path | str | None = None, sources_file: Path | str | None = None) -> None:
        if db_path is None or sources_file is None:
            cfg = load_config()
        self.db_path: Path = Path(db_path) if db_path is not None else cfg.state_db
        self.sources_file: Path = (
            Path(sources_file) if sources_file is not None else cfg.sources_file
        )

    @property
    def available(self) -> bool:
        return self.db_path.is_file()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection | None]:
        if not self.available:
            yield None
            return
        conn = sqlite3.connect(
            f"{self.db_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def _source_registry(self) -> list[dict]:
        try:
            data = json.loads(self.sources_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        sources = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(sources, list):
            return []
        result: list[dict] = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id") or "").strip()
            if not source_id:
                continue
            result.append(
                {
                    "source_id": source_id,
                    "name": str(item.get("name") or source_id).strip() or source_id,
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        return result

    def _source_names(self) -> dict[str, str]:
        return {item["source_id"]: item["name"] for item in self._source_registry()}

    def _sources(self, conn: sqlite3.Connection, names: dict[str, str]) -> list[dict]:
        feeds = {
            str(row["source_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT source_id, last_fetched_at, last_status, last_error,
                       consecutive_failures
                FROM feeds
                """
            )
        }
        result: list[dict] = []
        seen: set[str] = set()
        for item in self._source_registry():
            source_id = item["source_id"]
            seen.add(source_id)
            feed = feeds.get(source_id, {})
            result.append(
                {
                    "source_id": source_id,
                    "name": item["name"],
                    "enabled": item["enabled"],
                    "last_fetched_at": feed.get("last_fetched_at"),
                    "last_status": feed.get("last_status"),
                    "last_error": _truncate(feed.get("last_error"), 300) or None,
                    "consecutive_failures": _as_int(feed.get("consecutive_failures")),
                }
            )
        # 状态库里出现过、但注册表里没有的源也保留，避免数据“消失”。
        for source_id, feed in sorted(feeds.items()):
            if source_id in seen:
                continue
            result.append(
                {
                    "source_id": source_id,
                    "name": names.get(source_id, source_id),
                    "enabled": True,
                    "last_fetched_at": feed.get("last_fetched_at"),
                    "last_status": feed.get("last_status"),
                    "last_error": _truncate(feed.get("last_error"), 300) or None,
                    "consecutive_failures": _as_int(feed.get("consecutive_failures")),
                }
            )
        return result

    def _runs(self, conn: sqlite3.Connection, names: dict[str, str], limit: int) -> list[dict]:
        rows = list(
            conn.execute(
                """
                SELECT run_id, started_at, finished_at, mode, summary_json, exit_code
                FROM runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )
        if not rows:
            return []

        run_ids = [str(row["run_id"]) for row in rows]
        placeholders = ",".join("?" for _ in run_ids)
        source_rows: dict[str, list[dict]] = {}
        for row in conn.execute(
            f"""
            SELECT run_id, source_id, fetched, candidates, filtered,
                   ingested, skipped, failed, error
            FROM run_sources
            WHERE run_id IN ({placeholders})
            ORDER BY source_id
            """,
            run_ids,
        ):
            source_rows.setdefault(str(row["run_id"]), []).append(
                {
                    "source_id": str(row["source_id"]),
                    "source_name": names.get(str(row["source_id"]), str(row["source_id"])),
                    "fetched": _as_int(row["fetched"]),
                    "candidates": _as_int(row["candidates"]),
                    "filtered": _as_int(row["filtered"]),
                    "ingested": _as_int(row["ingested"]),
                    "skipped": _as_int(row["skipped"]),
                    "failed": _as_int(row["failed"]),
                    "error": _truncate(row["error"], 500) or None,
                }
            )

        result: list[dict] = []
        for row in rows:
            summary = _parse_summary(row["summary_json"])
            sources = source_rows.get(str(row["run_id"]), [])

            def total(key: str) -> int:
                value = summary.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return int(value)
                # run_sources 没有 pending 列，缺字段按 0 处理
                return sum(item.get(key, 0) for item in sources)

            result.append(
                {
                    "run_id": str(row["run_id"]),
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "mode": row["mode"],
                    "exit_code": (
                        _as_int(row["exit_code"])
                        if row["exit_code"] is not None
                        else (int(summary["exit_code"]) if isinstance(summary.get("exit_code"), (int, float)) else None)
                    ),
                    "elapsed_s": _as_float(summary.get("elapsed_s")),
                    "fetched": total("fetched"),
                    "candidates": total("candidates"),
                    "filtered": total("filtered"),
                    "expired": total("expired"),
                    "ingested": total("ingested"),
                    "skipped": total("skipped"),
                    "failed": total("failed"),
                    "pending": total("pending"),
                    "sources_ok": _as_int(summary.get("sources_ok"), len(sources)),
                    "sources_failed": _as_int(summary.get("sources_failed")),
                    "fatal_error": _truncate(summary.get("fatal_error"), 500) or None,
                    "sources": sources,
                }
            )
        return result

    # ------------------------------------------------------------------
    def overview(self, limit: int = 10) -> dict:
        limit = max(1, min(_as_int(limit, 10), 50))
        generated_at = now_iso()
        if not self.available:
            return {
                "available": False,
                "generated_at": generated_at,
                "counts": {**{status: 0 for status in STATUSES}, "total": 0},
                "sources": self._source_registry(),
                "runs": [],
            }

        with self._connect() as conn:
            assert conn is not None
            counts = {status: 0 for status in STATUSES}
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM articles GROUP BY status"):
                status = str(row["status"])
                if status in counts:
                    counts[status] = _as_int(row["n"])
            counts["total"] = sum(counts.values())
            names = self._source_names()
            return {
                "available": True,
                "generated_at": generated_at,
                "counts": counts,
                "sources": self._sources(conn, names),
                "runs": self._runs(conn, names, limit),
            }

    # ------------------------------------------------------------------
    def _list_item(self, row: sqlite3.Row, names: dict[str, str]) -> dict:
        summary = (row["summary_zh"] or "").strip()
        if summary:
            excerpt = _truncate(summary, 240)
        else:
            feed = html_fragment_to_text((row["feed_summary"] or "")[:4000], max_chars=400)
            excerpt = _truncate(feed, 240)
        source_id = str(row["source_id"])
        return {
            "id": _as_int(row["id"]),
            "source_id": source_id,
            "source_name": names.get(source_id, source_id),
            "title": row["title"] or "",
            "title_zh": row["title_zh"] or "",
            "summary_zh": summary,
            "excerpt": excerpt,
            "status": row["status"],
            "category": row["category"],
            "importance": _as_int(row["importance"]) if row["importance"] is not None else None,
            "published_at": row["published_at"],
            "fetched_at": row["fetched_at"],
            "doc_filename": row["doc_filename"],
            "canonical_url": row["canonical_url"],
            "reason": _truncate(row["reason"], 300) or None,
            "last_error": _truncate(row["last_error"], 500) or None,
        }

    def items(
        self,
        *,
        status: str | None = None,
        source_id: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        page = max(1, _as_int(page, 1))
        page_size = max(1, min(_as_int(page_size, 20), 100))
        status = (status or "").strip() or None
        source_id = (source_id or "").strip() or None
        q = (q or "").strip()[:200] or None

        if status == "all":
            status = None
        if status is not None and status not in STATUSES:
            raise ValueError(f"不支持的 status: {status}")
        if source_id is not None and not _SOURCE_ID_RE.match(source_id):
            raise ValueError("source_id 只能包含字母、数字、下划线和连字符")

        base = {
            "available": self.available,
            "page": page,
            "page_size": page_size,
            "total": 0,
            "items": [],
        }
        if not self.available:
            return base

        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source_id:
            clauses.append("source_id = ?")
            params.append(source_id)
        if q:
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR title_zh LIKE ? ESCAPE '\\' "
                "OR summary_zh LIKE ? ESCAPE '\\')"
            )
            pattern = _like_pattern(q)
            params.extend([pattern, pattern, pattern])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._connect() as conn:
            assert conn is not None
            total = _as_int(
                conn.execute(f"SELECT COUNT(*) FROM articles{where}", params).fetchone()[0]
            )
            names = self._source_names()
            rows = list(
                conn.execute(
                    f"""
                    SELECT {_LIST_COLUMNS}
                    FROM articles{where}
                    ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*params, page_size, (page - 1) * page_size],
                )
            )
            base.update(
                {
                    "total": total,
                    "items": [self._list_item(row, names) for row in rows],
                }
            )
        return base

    # ------------------------------------------------------------------
    def detail(self, item_id: int) -> dict | None:
        if not self.available:
            return None
        with self._connect() as conn:
            assert conn is not None
            row = conn.execute("SELECT * FROM articles WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            names = self._source_names()
            data = self._list_item(row, names)
            doc_text = (row["doc_text"] or "").strip()
            if doc_text:
                content, content_kind = doc_text, "document"
            else:
                feed_text = html_fragment_to_text(row["feed_summary"] or "", max_chars=30000)
                content = feed_text.strip()
                content_kind = "feed" if content else "none"
            data.update(
                {
                    "key_facts": _parse_key_facts(row["key_facts_json"]),
                    "content": content[:30000],
                    "content_kind": content_kind,
                    "ingest_result": _truncate(row["ingest_result"], 1000) or None,
                    "job_id": row["job_id"],
                    "retry_count": _as_int(row["retry_count"]),
                    "next_retry_at": row["next_retry_at"],
                    "updated_at": row["updated_at"],
                }
            )
            return data
