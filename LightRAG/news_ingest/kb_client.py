"""通过运行中的 FastAPI 入库，绝不当第二个 LightRAG 写入者。

接口：
  POST /v1/kb/upload          multipart 字段 file
  GET  /v1/kb/ingest/{job_id}
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen as _stdlib_urlopen

from .config import NewsConfig


class KBError(RuntimeError):
    pass


class KBAuthError(KBError):
    pass


class KBBusy(KBError):
    pass


class KBPermanentError(KBError):
    pass


class KBTransientError(KBError):
    pass


class KBJobExpired(KBError):
    pass


class KBJobTimeout(KBError):
    pass


@dataclass(frozen=True)
class JobResult:
    job_id: str
    status: str  # ingested / skipped / failed
    result: str
    stage: str = ""


def encode_multipart(filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----LightRAGNewsIngest" + uuid.uuid4().hex
    safe_name = (filename or "article.txt").replace('"', "_").replace("\r", "").replace("\n", "")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def classify_result(result: str) -> str:
    value = (result or "").strip()
    if value.startswith("[成功]"):
        return "ingested"
    if value.startswith("[跳过]"):
        return "skipped"
    if value.startswith("[失败]"):
        return "failed"
    # 空结果/未知结果绝不当作成功
    return "failed"


class KBClient:
    def __init__(
        self,
        cfg: NewsConfig,
        *,
        urlopen: Callable | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._urlopen = urlopen or _stdlib_urlopen
        self._sleeper = sleeper
        self._clock = clock

    # ------------------------------------------------------------------
    def health(self) -> bool:
        try:
            response = self._urlopen(
                Request(
                    f"{self.cfg.api_base_url}/health",
                    headers=self._headers(accept="application/json"),
                    method="GET",
                ),
                timeout=5.0,
            )
            try:
                status = int(getattr(response, "status", None) or response.getcode() or 0)
                return 200 <= status < 300
            finally:
                self._close(response)
        except Exception:
            return False

    def submit(self, filename: str, content: bytes) -> str:
        body, content_type = encode_multipart(filename, content)
        headers = self._headers(accept="application/json")
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                response = self._urlopen(
                    Request(
                        f"{self.cfg.api_base_url}/v1/kb/upload",
                        data=body,
                        headers=headers,
                        method="POST",
                    ),
                    timeout=max(self.cfg.http_timeout_s, 30.0),
                )
                try:
                    payload = self._read_json(response)
                finally:
                    self._close(response)
                job_id = payload.get("job_id") if isinstance(payload, dict) else None
                if not isinstance(job_id, str) or not job_id:
                    raise KBPermanentError("上传响应缺少 job_id")
                return job_id
            except HTTPError as exc:
                try:
                    self._raise_for_http(exc)
                except KBTransientError as transient:
                    last_exc = transient
                    if attempt == 0:
                        self._sleeper(2.0)
                        continue
                    raise
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt == 0:
                    self._sleeper(2.0)
                    continue
                raise KBTransientError(f"上传连接失败: {exc}") from exc
        raise KBTransientError(f"上传失败: {last_exc}")

    def submit_waiting_for_lock(self, filename: str, content: bytes) -> str:
        """上传，遇到 409 时等待而不是抢锁；返回 job_id。"""
        deadline = self._clock() + self.cfg.conflict_max_wait_s
        while True:
            try:
                return self.submit(filename, content)
            except KBBusy:
                if self._clock() >= deadline:
                    raise KBBusy("入库任务持续被占用，等待超时")
                self._sleeper(self.cfg.conflict_wait_s)

    def submit_and_wait(self, filename: str, content: bytes) -> JobResult:
        return self.wait(self.submit_waiting_for_lock(filename, content))

    def get_job(self, job_id: str) -> dict | None:
        try:
            response = self._urlopen(
                Request(
                    f"{self.cfg.api_base_url}/v1/kb/ingest/{job_id}",
                    headers=self._headers(accept="application/json"),
                    method="GET",
                ),
                timeout=max(self.cfg.http_timeout_s, 10.0),
            )
        except HTTPError as exc:
            if exc.code == 404:
                return None
            self._raise_for_http(exc)
            raise  # pragma: no cover
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise KBTransientError(f"查询入库任务失败: {exc}") from exc
        try:
            payload = self._read_json(response)
        finally:
            self._close(response)
        return payload if isinstance(payload, dict) else None

    def wait(self, job_id: str) -> JobResult:
        deadline = self._clock() + self.cfg.job_timeout_s
        while True:
            job = self.get_job(job_id)
            if job is None:
                raise KBJobExpired(f"入库任务不存在或已过期: {job_id}")
            if job.get("done"):
                result = str(job.get("result") or "")
                return JobResult(
                    job_id=job_id,
                    status=classify_result(result),
                    result=result,
                    stage=str(job.get("stage") or ""),
                )
            if self._clock() >= deadline:
                raise KBJobTimeout(f"等待入库任务超时: {job_id}")
            self._sleeper(self.cfg.poll_interval_s)

    # ------------------------------------------------------------------
    def _headers(self, *, accept: str) -> dict[str, str]:
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Accept": accept,
        }
        if self.cfg.api_key:
            headers["X-API-Key"] = self.cfg.api_key
        return headers

    def _read_json(self, response) -> dict:
        try:
            raw = response.read(1024 * 1024)
        except Exception as exc:
            raise KBTransientError(f"读取响应失败: {exc}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KBTransientError("服务端返回不是合法 JSON") from exc

    def _raise_for_http(self, exc: HTTPError) -> None:
        status = int(exc.code)
        if status == 409:
            raise KBBusy("入库互斥锁被占用（409）") from exc
        if status in (401, 403):
            raise KBAuthError(f"API 鉴权失败（HTTP {status}）") from exc
        if status == 422:
            raise KBPermanentError("服务端拒绝该文件（HTTP 422）") from exc
        if status >= 500:
            raise KBTransientError(f"服务端错误（HTTP {status}）") from exc
        raise KBPermanentError(f"上传失败（HTTP {status}）") from exc

    @staticmethod
    def _close(response) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()
