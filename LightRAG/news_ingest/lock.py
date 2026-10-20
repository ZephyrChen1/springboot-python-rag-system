"""跨平台单实例文件锁。

不要用 PID + os.kill(pid, 0) 判断存活：Windows 上某些 Python
实现/权限组合可能直接终止目标进程。OS 文件锁在进程退出时自动释放。

锁的粒度：
- **跨进程**：OS 文件锁（msvcrt / flock），进程崩溃时自动释放；
- **跨线程**：Windows 的字节范围锁属于进程而非句柄，同进程的两个线程
  用不同句柄锁同一区域会同时成功。因此在文件锁之外再加一把进程内的
  `threading.Lock`，否则同进程并发调用（如会话历史的多线程写入）会失效。

默认非阻塞（crawler 的 --once 需要在已有实例时立刻退出）；
需要排队时传 timeout，内部按 50ms 重试。
"""

from __future__ import annotations

import json
import os
import threading
import time
import weakref
from pathlib import Path


class LockBusy(RuntimeError):
    pass


# 同一路径在进程内共用一把线程锁。弱引用：持有/排队中的锁有强引用不会丢，
# 没人用的会话锁会被回收，避免长跑进程按 session 数无限增长。
_PROCESS_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = (
    weakref.WeakValueDictionary()
)
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(key: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


class RunLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh = None
        self._tlock: threading.Lock | None = None

    def acquire(self, timeout: float = 0.0) -> "RunLock":
        """获取锁；timeout<=0 表示只尝试一次，>0 表示最多等待这么多秒。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout)

        # 先抢进程内线程锁，保证同进程的并发调用也被串行化
        tlock = _process_lock(str(self.path.resolve()))
        if timeout > 0:
            got = tlock.acquire(timeout=timeout)
        else:
            got = tlock.acquire(blocking=False)
        if not got:
            raise LockBusy(f"lock is held by another thread: {self.path}")
        self._tlock = tlock

        try:
            self._acquire_file(deadline)
        except BaseException:
            self._tlock = None
            tlock.release()
            raise
        return self

    def _acquire_file(self, deadline: float) -> None:
        self._fh = open(self.path, "a+", encoding="utf-8")
        # msvcrt/flock 都需要锁区存在；空文件先补一个字节。
        # 必须在加锁前写入，否则别的进程持锁时这里的 write/flush 会被拒绝。
        if os.fstat(self._fh.fileno()).st_size == 0:
            try:
                self._fh.write("\n")
                self._fh.flush()
            except OSError:
                # 极端情况下别人正持有锁（文件被截断），交给后面的加锁逻辑报忙
                pass

        while True:
            try:
                self._fh.seek(0)
                self._lock_fd()
                break
            except (OSError, IOError) as exc:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise LockBusy(
                        f"lock is held by another process: {exc}"
                    ) from exc
                time.sleep(0.05)

        # 拿到锁之后再写诊断信息，避免未持锁的进程改写占用者内容
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(
                json.dumps(
                    {"pid": os.getpid(), "path": str(self.path)},
                    ensure_ascii=False,
                )
                + "\n"
            )
            self._fh.flush()
            self._fh.seek(0)
        except OSError:
            pass

    def _lock_fd(self) -> None:
        assert self._fh is not None
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        try:
            if self._fh is not None:
                try:
                    if os.name == "nt":
                        import msvcrt

                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    self._fh.close()
                    self._fh = None
        finally:
            tlock = self._tlock
            self._tlock = None
            if tlock is not None:
                tlock.release()

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
