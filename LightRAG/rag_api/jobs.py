"""入库任务注册表（SQLite）。

原先任务状态放在 `rag_api.ingest` 的进程内 dict 里，带来两个问题：

- API 重启后 `GET /v1/kb/ingest/{job_id}` 一律 404，调用方只能重传；
- 多进程部署时请求会落到没处理过该任务的进程，仍然 404。

现在状态落在 SQLite（WAL）。连接按调用创建、用完即关：
写频率极低（每次入库一条），没必要维护长连接和跨线程锁。
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import config_data as config

# 已结束任务保留时间与数量：agent 常常「提交 -> 先回答 -> 下一轮再问进度」，
# 只记当前任务会让 kb_status 找不到刚完成的那一个。
JOB_TTL_S = 3600
JOBS_MAX_KEPT = 20

STATUS_RUNNING = "running"
STATUS_DONE = "done"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_jobs (
  job_id TEXT PRIMARY KEY,
  file_name TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_file
  ON ingest_jobs(file_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status
  ON ingest_jobs(status, started_at DESC);
"""


def _db_path(path: str | Path | None = None) -> Path:
    return Path(path if path is not None else config.ingest_job_db)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout：多个进程同时读/偶尔写时不会立刻 database is locked
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    return conn


@contextmanager
def _conn(path: Path):
    """连接用完即关；Windows 下未关闭的句柄会挡住临时目录清理。"""
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _row_to_job(row: sqlite3.Row) -> dict:
    return {
        "job_id": str(row["job_id"]),
        "file_name": str(row["file_name"]),
        "done": row["status"] == STATUS_DONE,
        "result": row["result"] or "",
        "started_at": float(row["started_at"]),
        "finished_at": float(row["finished_at"]) if row["finished_at"] is not None else None,
    }


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute(
        "DELETE FROM ingest_jobs WHERE status=? AND COALESCE(finished_at, started_at) < ?",
        (STATUS_DONE, now - JOB_TTL_S),
    )
    overflow = conn.execute(
        "SELECT COUNT(*) FROM ingest_jobs WHERE status=?", (STATUS_DONE,)
    ).fetchone()[0] - JOBS_MAX_KEPT
    if overflow > 0:
        conn.execute(
            """
            DELETE FROM ingest_jobs WHERE job_id IN (
              SELECT job_id FROM ingest_jobs WHERE status=?
              ORDER BY COALESCE(finished_at, started_at) ASC LIMIT ?
            )
            """,
            (STATUS_DONE, overflow),
        )
    conn.commit()


def create_job(file_name: str, *, db_path: str | Path | None = None) -> dict:
    job_id = uuid.uuid4().hex
    now = time.time()
    with _conn(_db_path(db_path)) as conn:
        _prune(conn, now)
        conn.execute(
            "INSERT INTO ingest_jobs (job_id, file_name, status, result, started_at) "
            "VALUES (?, ?, ?, '', ?)",
            (job_id, file_name, STATUS_RUNNING, now),
        )
        conn.commit()
    return {
        "job_id": job_id,
        "file_name": file_name,
        "done": False,
        "result": "",
        "started_at": now,
        "finished_at": None,
    }


def finish_job(
    job_id: str, result: str, *, db_path: str | Path | None = None
) -> None:
    with _conn(_db_path(db_path)) as conn:
        conn.execute(
            "UPDATE ingest_jobs SET status=?, result=?, finished_at=? WHERE job_id=?",
            (STATUS_DONE, result, time.time(), job_id),
        )
        conn.commit()


def get_job(job_id: str, *, db_path: str | Path | None = None) -> dict | None:
    with _conn(_db_path(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM ingest_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row is not None else None


def latest_running(*, db_path: str | Path | None = None) -> dict | None:
    with _conn(_db_path(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM ingest_jobs WHERE status=? ORDER BY started_at DESC LIMIT 1",
            (STATUS_RUNNING,),
        ).fetchone()
        return _row_to_job(row) if row is not None else None


def find_latest_by_file_name(
    file_name: str, *, db_path: str | Path | None = None
) -> dict | None:
    """按 basename 找最近一次任务（含已完成的）。

    用户/模型通常只知道文件名；正在入库的文件还没写进 doc_status，
    只看 doc_status 会误报「未找到」。
    """
    target = os.path.basename(str(file_name or "").replace("\\", "/"))
    if not target:
        return None
    with _conn(_db_path(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM ingest_jobs ORDER BY started_at DESC LIMIT 200"
        ).fetchall()
    for row in rows:
        name = os.path.basename(str(row["file_name"] or "").replace("\\", "/"))
        if name == target:
            return _row_to_job(row)
    return None


def fail_stale_running(
    reason: str = "[失败]入库进程中断，任务未完成", *, db_path: str | Path | None = None
) -> int:
    """把遗留的 running 任务标成失败。

    只能在**已经拿到跨进程写锁**时调用：此时不可能有真正在跑的任务，
    残留的 running 行一定是上次进程崩溃留下的。
    """
    with _conn(_db_path(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE ingest_jobs SET status=?, result=?, finished_at=? WHERE status=?",
            (STATUS_DONE, reason, time.time(), STATUS_RUNNING),
        )
        conn.commit()
        return cursor.rowcount
