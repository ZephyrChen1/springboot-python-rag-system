
import threading
from knowledge_base import KnowledgeBaseService, read_ingest_progress, describe_ingest_stage
import time
import config_data as config
import uuid

_active_job: dict | None = None
_lock = threading.Lock() #互斥锁，防止LightRAG同时入库出问题

class IngestBusyError(Exception):
    """已有入库任务在跑。参数为占用任务的文件名。"""

def submit_ingest(filename: str, content: bytes) -> dict:
    """提交入库任务，立即返回 job 摘要；已有任务在跑则抛 IngestBusyError。"""
    global _active_job
    with _lock:
        if _active_job is not None and not _active_job["done"]:
            raise IngestBusyError(_active_job["file_name"])

        service = KnowledgeBaseService()
        job = {
            "job_id": uuid.uuid4().hex,
            "file_name": filename,
            "done": False,
            "result": "",
            "started_at": time.time(),
        }

        def _target():
            try:
                job["result"] = service.upload_file(filename, content)
            except Exception as exc:
                job["result"] = f"[失败]入库出错：{exc}"
            finally:
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

def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _active_job
        if job is None or job["job_id"] != job_id:
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
