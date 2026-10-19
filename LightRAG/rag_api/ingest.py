
import asyncio
import os
import threading
from knowledge_base import KnowledgeBaseService, read_ingest_progress, describe_ingest_stage
import time
import config_data as config
import uuid

_active_job: dict | None = None
# 已结束的任务也要保留一段时间：agent 常常是「提交 -> 先回答用户 -> 下一轮
# 再问进度」，只记当前任务会让 kb_status 找不到刚刚完成的那一个。
_jobs: dict[str, dict] = {}
_JOBS_MAX_KEPT = 20
_JOB_TTL_S = 3600
_lock = threading.Lock() #互斥锁，防止LightRAG同时入库出问题


def _prune_jobs_locked(now: float | None = None) -> None:
    """清理过期/超量的已完成任务。调用方必须持有 _lock。"""
    now = now or time.time()
    for job_id, job in list(_jobs.items()):
        # 用完成时间而不是开始时间：一次超过 1 小时的入库，刚完成就该能被查到。
        ended_at = job.get("finished_at") or job["started_at"]
        if job["done"] and now - ended_at > _JOB_TTL_S:
            _jobs.pop(job_id, None)

    overflow = len(_jobs) - _JOBS_MAX_KEPT
    if overflow <= 0:
        return
    finished = sorted(
        (job for job in _jobs.values() if job["done"]),
        key=lambda job: job.get("finished_at") or job["started_at"],
    )
    for job in finished[:overflow]:
        _jobs.pop(job["job_id"], None)


class IngestBusyError(Exception):
    """已有入库任务在跑。参数为占用任务的文件名。"""

def submit_ingest(filename: str, content: bytes) -> dict:
    """提交入库任务，立即返回 job 摘要；已有任务在跑则抛 IngestBusyError。"""
    global _active_job
    with _lock:
        if _active_job is not None and not _active_job["done"]:
            raise IngestBusyError(_active_job["file_name"])

        _prune_jobs_locked()
        service = KnowledgeBaseService()
        job = {
            "job_id": uuid.uuid4().hex,
            "file_name": filename,
            "done": False,
            "result": "",
            "started_at": time.time(),
        }
        _jobs[job["job_id"]] = job

        def _target():
            try:
                job["result"] = service.upload_file(filename, content)
            except Exception as exc:
                job["result"] = f"[失败]入库出错：{exc}"
            finally:
                job["finished_at"] = time.time()
                # 先写完成时间、再置 done：读线程看到 done=True 时
                # finished_at 一定已经存在。
                job["done"] = True

        #将入库任务放到子线程去，防止主线程被阻塞
        t = threading.Thread(target=_target, daemon=True, name="kb-ingest")
        t.start()
        _active_job = job

        return {
            "job_id": job["job_id"],
            "file_name": job["file_name"],
            "done": job["done"],
        }

def find_job_by_file_name(file_name: str) -> dict | None:
    """按文件名找最近一次入库任务（含已完成的），没有则返回 None。

    用户/模型通常只知道文件名。正在入库的文件还没写进 doc_status，
    只看 doc_status 会误报「未找到」。
    """
    target = os.path.basename(str(file_name or "").replace("\\", "/"))
    if not target:
        return None
    with _lock:
        _prune_jobs_locked()
        matches = [
            job for job in _jobs.values()
            if os.path.basename(str(job.get("file_name") or "").replace("\\", "/")) == target
        ]
        newest = max(matches, key=lambda j: j.get("started_at", 0), default=None)
        job_id = newest["job_id"] if newest else None
    # get_job 自己还要拿锁，不能在持有 _lock 时调用（普通 Lock 不可重入）
    return get_job(job_id) if job_id else None


def get_job(job_id: str) -> dict | None:
    with _lock:
        _prune_jobs_locked()
        job = _jobs.get(job_id)
        if job is None:
            return None
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

    供 agent 工具在常驻 event loop 上调用。这里用 1 秒轮询而不是 Condition：
    任务状态由子线程更新，跨线程通知的复杂度换不来 1 秒的收益。

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
