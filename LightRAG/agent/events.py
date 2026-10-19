"""事件协议的唯一产出点。

上游以 NDJSON 输出（一行一个 JSON 对象）。所有事件字段的增减只能改这里，
不要在 loop / tools 里手拼字典，否则协议会很快出现两套实现。
"""

import json
import time

import config_data as config

# 事件协议版本。只有破坏性变更才递增。
PROTOCOL_VERSION = 1


def _base(ctx, type_: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": type_,
        "run_id": ctx.run_id,
        "ts": int(time.time() * 1000),
    }


def start(ctx) -> dict:
    ev = _base(ctx, "start")
    ev.update(session_id=ctx.session_id, user_id=ctx.user_id)
    return ev


def thought(ctx, step: int, text: str) -> dict:
    ev = _base(ctx, "thought")
    ev.update(step=step, text=text)
    return ev


def tool_call(ctx, step: int, call_id: str, name: str, args: dict) -> dict:
    ev = _base(ctx, "tool_call")
    ev.update(step=step, call_id=call_id, name=name, args=args)
    return ev


def tool_result(
    ctx, step: int, call_id: str, result, elapsed_ms: int | None = None
) -> dict:
    """result 是 ToolResult。data 序列化后超过 2KB 就不下发细节。"""
    data = result.data
    truncated = False
    if data is not None:
        raw = json.dumps(data, ensure_ascii=False, default=str)
        if len(raw) > 2048:
            data = None
            truncated = True

    ev = _base(ctx, "tool_result")
    ev.update(
        step=step,
        call_id=call_id,
        ok=result.ok,
        summary=result.summary[: config.agent_tool_result_max_chars],
        data=data,
        truncated=truncated,
    )
    # 由工具执行侧实测的耗时。前端优先用它，避免跨服务时钟偏差。
    if elapsed_ms is not None:
        ev["elapsed_ms"] = max(0, int(elapsed_ms))
    if result.error:
        ev["error"] = result.error
    return ev


def answer(ctx, delta: str) -> dict:
    ev = _base(ctx, "answer")
    ev.update(delta=delta)
    return ev


def confirm_required(ctx, call_id: str, name: str, args: dict, token: str, expires_at: float) -> dict:
    ev = _base(ctx, "confirm_required")
    ev.update(
        call_id=call_id,
        name=name,
        args=args,
        token=token,
        expires_at=expires_at,
    )
    return ev


def done(
    ctx,
    steps: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> dict:
    """结束事件。

    usage 是本次 run 的累计值：每一轮 planning 调用 + 最后一次流式回答。
    两个计数都为 None 时表示上游没有返回 usage。
    """
    usage = None
    if prompt_tokens is not None or completion_tokens is not None:
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    ev = _base(ctx, "done")
    ev.update(steps=steps, usage=usage, finish_reason="stop")
    return ev


def error(ctx, code: str, message: str, retryable: bool = False) -> dict:
    ev = _base(ctx, "error")
    ev.update(code=code, message=message[:500], retryable=retryable)
    return ev


def to_line(event: dict) -> str:
    """序列化成 NDJSON 的一行（含换行符）。

    ensure_ascii=False 保留中文可读性；json.dumps 会把内容里的换行转义成 \\n，
    因此不会破坏「一行一个 JSON」的约定。
    """
    return json.dumps(event, ensure_ascii=False, default=str) + "\n"
