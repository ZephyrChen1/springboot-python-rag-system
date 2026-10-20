"""Agent 会话记忆。

只存「用户问题 + 最终回答」，不存工具调用与观察结果：
  - 下一轮需要的是结论，不是上一轮检索到的几千字原文；
  - 工具结果来自文档内容，反复回放会扩大提示注入面。
工具步骤的价值在可排查，所以进 trace（见 trace.py），不进对话历史。

文件与旧链路物理隔离：agent_<session_id>.json，绝不覆盖 chat_history/<session_id>。
"""

import json
import os
import tempfile

import config_data as config
from file_history_store import _SESSION_ID_RE
from news_ingest.lock import RunLock

_DIR = "./chat_history"
# append_turn 是「读-改-写」，同一 session 的并发 run 会丢掉其中一轮。
# 文件锁同时管住跨线程和跨进程（多 worker 时也成立）。
_MEMORY_LOCK_TIMEOUT_S = 10.0


def _validate(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"非法的 session_id: {session_id}"
            "（仅允许字母/数字/下划线/连字符，长度 1-128）"
        )
    return session_id


def _path(session_id: str) -> str:
    return os.path.join(_DIR, f"agent_{_validate(session_id)}.json")


def _read_all(session_id: str) -> list[dict]:
    try:
        with open(_path(session_id), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [
        item for item in data
        if isinstance(item, dict) and item.get("role") in ("user", "assistant")
    ]


def _truncate(text: str) -> str:
    """按字符数截断单条消息。

    历史里存的是最终回答，长度没有上限；10 轮长回答足以让下一轮的
    planning 调用直接撞上模型上下文上限。写入时截断是第一道闸，
    load 时再截一次，兼容本次改动之前落盘的旧文件。
    """
    text = text if isinstance(text, str) else ""
    limit = config.agent_history_max_chars
    if limit <= 0 or len(text) <= limit:
        return text
    head = max(0, limit - len("…（已截断）"))
    return text[:head] + "…（已截断）"


def load(session_id: str, limit: int | None = None) -> list[dict]:
    """返回可回放的 messages（已按轮数和单条长度截断）。"""
    messages = _read_all(session_id)
    limit = limit or config.agent_history_max_turns * 2
    return [
        {**m, "content": _truncate(m.get("content", ""))}
        for m in messages[-limit:]
    ]


def append_turn(session_id: str, question: str, answer: str) -> None:
    """追加一轮问答。answer 为空也写入，用于记录一次失败。"""
    _validate(session_id)
    lock = RunLock(os.path.join(_DIR, ".locks", f"agent_{session_id}.lock"))
    lock.acquire(timeout=_MEMORY_LOCK_TIMEOUT_S)
    try:
        messages = _read_all(session_id)
        messages.append({"role": "user", "content": _truncate(question)})
        messages.append({"role": "assistant", "content": _truncate(answer or "")})
        messages = messages[-config.agent_history_max_turns * 2:]

        path = _path(session_id)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        # 原子写：避免进程中断留下半个 JSON，导致下一轮读不出历史
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    finally:
        lock.release()
