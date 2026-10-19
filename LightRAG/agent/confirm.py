"""写操作的人工确认（HITL）。

进程内实现，不跨请求持久化：
  - agent 循环在常驻 event loop 上创建一个 PendingConfirm，然后轮询等待；
  - /ai/agent/confirm 在 FastAPI 线程池里调用 resolve()，用 threading.Event 通知；
  - 超时或拒绝 -> 工具不执行。

为什么不用 asyncio.Event 直接跨线程 set：
事件分属两个线程（线程池 vs 常驻 loop），asyncio.Event 不是线程安全的。
轮询 200ms 的代价远小于跨 loop 调度的出错风险。
"""

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class PendingConfirm:
    token: str
    name: str
    args: dict
    expires_at: float
    approved: bool = False
    _event: threading.Event = field(default_factory=threading.Event)


_PENDING: dict[str, PendingConfirm] = {}
_LOCK = threading.Lock()


def create(name: str, args: dict, ttl_s: int) -> PendingConfirm:
    token = uuid.uuid4().hex
    pending = PendingConfirm(
        token=token,
        name=name,
        args=dict(args or {}),
        expires_at=time.time() + max(1, ttl_s),
    )
    with _LOCK:
        _PENDING[token] = pending
    return pending


def resolve(token: str, approved: bool) -> bool:
    """由 /ai/agent/confirm 调用。返回 False 表示 token 不存在或已过期。"""
    with _LOCK:
        pending = _PENDING.get(token)
        if pending is None:
            return False
        if time.time() > pending.expires_at:
            _PENDING.pop(token, None)
            return False
        pending.approved = bool(approved)
        pending._event.set()
        return True


async def wait(pending: PendingConfirm) -> bool:
    """在 agent 循环里等用户确认。超时或被拒绝都返回 False。"""
    try:
        while not pending._event.is_set() and time.time() < pending.expires_at:
            await asyncio.sleep(0.2)
        return pending._event.is_set() and pending.approved
    finally:
        # 必须放在 finally：消费者断连导致任务被 cancel 时，token 也要清掉，
        # 否则它会一直挂在 _PENDING 里直到 TTL，confirm 接口还能“批准”一个
        # 已经没人等待的写操作。
        with _LOCK:
            _PENDING.pop(pending.token, None)


def cleanup_expired() -> None:
    """可由定时任务调用；不调用也不会泄漏太多（TTL 短、数量少）。"""
    now = time.time()
    with _LOCK:
        for token, pending in list(_PENDING.items()):
            if now > pending.expires_at:
                _PENDING.pop(token, None)
