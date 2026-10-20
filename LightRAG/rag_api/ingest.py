"""入库任务调度：跨进程写锁 + 持久化任务状态。

两条约束分开看：

1. **同一时刻只能有一个 KB 写操作**（入库/删除）。LightRAG 的
   KV/doc_status 是文件型存储、每个进程各持一份内存快照，两个进程并发写
   会互相覆盖。互斥用 OS 文件锁实现（`news_ingest.lock.RunLock`），
   进程崩溃时锁自动释放，不会像数据库行锁那样留下永久占用的脏状态。
2. **任务状态要跨进程可查**。状态落在 SQLite（`rag_api.jobs`），
   请求落到哪个进程都能回答 `GET /v1/kb/ingest/{job_id}`，API 重启也不丢。

注意：这两条并不等于「可以开多 worker」。LightRAG 实例仍是进程内单例、
工作目录仍是文件型存储，多 worker 下每个进程都会持有一份过期快照。
真正扩容需要把 KV/doc_status 换成共享后端（见 README「扩容路线」）。
"""

import asyncio
import threading
import time

import config_data as config
from knowledge_base import (
    KnowledgeBaseService,
    describe_ingest_stage,
    read_ingest_progress,
)
from news_ingest.lock import LockBusy, RunLock

from . import jobs

# 只保护本进程内的提交判断；跨进程互斥靠 ingest.lock。
_lock = threading.Lock()
_active_lock: RunLock | None = None
_active_job_id: str | None = None


class IngestBusyError(Exception):
    """已有入库任务在跑。参数为占用任务的文件名。"""


def acquire_kb_write_lock(timeout: float | None = None) -> RunLock:
    """获取 KB 写锁（入库与删除共用）。

    Raises:
        LockBusy: 超时仍被其它进程/线程占用。
    """
    lock = RunLock(config.ingest_lock_path)
    lock.acquire(
        timeout=config.kb_delete_lock_timeout_s if timeout is None else timeout
    )
    return lock


def _busy_file_name() -> str:
    running = jobs.latest_running()
    return running["file_name"] if running else "另一进程"


def _upload_worker(job_id: str, filename: str, content: bytes, lock: RunLock) -> None:
    global _active_lock, _active_job_id
    result = ""
    try:
        service = KnowledgeBaseService()
        result = service.upload_file(filename, content)
    except Exception as exc:
        result = f"[失败]入库出错：{exc}"
    finally:
        try:
            jobs.finish_job(job_id, result)
        finally:
            lock.release()
            with _lock:
                _active_lock = None
                _active_job_id = None


def submit_ingest(filename: str, content: bytes) -> dict:
    """提交入库任务，立即返回 job 摘要；已有任务在跑则抛 IngestBusyError。"""
    global _active_lock, _active_job_id
    with _lock:
        if _active_job_id is not None:
            current = jobs.get_job(_active_job_id)
            if current is not None and not current["done"]:
                raise IngestBusyError(current["file_name"])

        try:
            lock = acquire_kb_write_lock(timeout=0)
        except LockBusy as exc:
            raise IngestBusyError(_busy_file_name()) from exc

        try:
            # 拿到锁说明没有真正在跑的任务，残留的 running 行来自上次崩溃。
            jobs.fail_stale_running()
            job = jobs.create_job(filename)
        except Exception:
            lock.release()
            raise

        _active_lock = lock
        _active_job_id = job["job_id"]
        try:
            threading.Thread(
                target=_upload_worker,
                args=(job["job_id"], filename, content, lock),
                daemon=True,
                name="kb-ingest",
            ).start()
        except Exception as exc:
            # 线程没起来就别把锁和 running 行留在那儿
            jobs.finish_job(job["job_id"], f"[失败]无法启动入库线程：{exc}")
            lock.release()
            _active_lock = None
            _active_job_id = None
            raise

        return {
            "job_id": job["job_id"],
            "file_name": job["file_name"],
            "done": False,
        }


def find_job_by_file_name(file_name: str) -> dict | None:
    """按文件名找最近一次入库任务（含已完成的），没有则返回 None。"""
    job = jobs.find_latest_by_file_name(file_name)
    return get_job(job["job_id"]) if job else None


def _reclaim_if_crashed() -> None:
    """有 running 行、但写锁空闲，说明上次进程在入库中途退出。

    只在**能拿到**写锁时判定：锁被持有就说明确实有任务在跑（可能是别的进程）。
    """
    probe = RunLock(config.ingest_lock_path)
    try:
        probe.acquire(timeout=0)
    except LockBusy:
        return
    try:
        jobs.fail_stale_running()
    finally:
        probe.release()


def get_job(job_id: str) -> dict | None:
    job = jobs.get_job(job_id)
    if job is None:
        return None
    if not job["done"]:
        # 崩溃后没人会把 running 改成 done，查询时顺手回收，
        # 否则调用方会一直轮询到超时。
        _reclaim_if_crashed()
        job = jobs.get_job(job_id) or job
    elapsed = time.time() - job["started_at"]
    progress = read_ingest_progress(config.lightrag_working_dir, job["file_name"])
    return {
        "job_id": job["job_id"],
        "file_name": job["file_name"],
        "done": job["done"],
        "result": job["result"],
        "stage": describe_ingest_stage(progress, elapsed),
        "elapsed_sec": round(elapsed, 1),
    }


async def wait_job(job_id: str, timeout: float = 900) -> dict:
    """轮询等待入库任务结束。

    供 agent 工具在常驻 event loop 上调用。任务状态由子线程写库，
    这里按 1 秒轮询；跨进程也一样有效（读的是 SQLite，不是进程内存）。

    Raises:
        KeyError: job 不存在（已完成任务保留 1 小时，最多 20 个）
        TimeoutError: 超过 timeout 仍未结束
    """
    deadline = time.time() + timeout
    while True:
        job = get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["done"]:
            return job
        if time.time() >= deadline:
            raise TimeoutError(f"入库任务 {job_id} 超过 {timeout}s 未完成")
        await asyncio.sleep(1.0)
