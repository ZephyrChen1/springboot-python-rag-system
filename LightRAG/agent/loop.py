"""ReAct 决策循环。

每一轮：
  1. 调模型（带 tools）；
  2. 没有 tool_calls -> 这是最终答案，重新以流式生成并逐字下发；
  3. 有 tool_calls -> 逐个执行，把 observation 塞回 messages，继续下一轮。

为什么最终回答要重新调一次：
规划调用是非流式的（才能稳定拿到 tool_calls）。当它返回正文且没有工具调用时，
直接把这坨正文按字切片是假流式，体验是「等很久 -> 瞬间刷完」。
重新发起一次 stream=True、不带 tools 的调用，换来真正的逐字输出，
代价是最后一步多一次生成。这个取舍写在 docs 里。
"""

import asyncio
import json
import time

import config_data as config
from . import confirm, events, llm, memory
from .prompt import build_system_prompt
from .tools import base as tools

# answer 是逐 token 的增量事件，一个回答能产生成百上千条。
# 每写一条都要 open/write/close 文件，且发生在 event loop 上，会把整个
# agent 的吞吐拖垮；它也没有排查价值（正文本身在 final answer 里）。
# trace 只保留决策轨迹与工具调用。
_TRACE_EVENT_TYPES = frozenset(
    {"start", "thought", "tool_call", "confirm_required", "tool_result", "error", "done"}
)


def _emit(ctx, event: dict) -> dict:
    if ctx.trace is not None and event.get("type") in _TRACE_EVENT_TYPES:
        ctx.trace.write(event)
    return event


def _add_usage(totals: dict, usage) -> None:
    """把一次调用的 usage 累加进 totals（缺失字段按 0 计）。"""
    if usage is None:
        return
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if isinstance(prompt_tokens, int):
        totals["prompt_tokens"] += prompt_tokens
    if isinstance(completion_tokens, int):
        totals["completion_tokens"] += completion_tokens


def _write_key(name: str, args: dict) -> tuple[str, str]:
    """写操作的身份：工具名 + 规范化参数。

    用户拒绝一次之后，模型常见的行为是换个说法再调一遍同样的写操作。
    用这个 key 短路掉同一 run 内完全相同的重试，避免它反复弹确认框。
    """
    try:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical = str(args)
    return name, canonical


async def run_agent(question: str, ctx, history: list[dict] | None = None):
    """异步生成事件字典。必须运行在 LightRAGService 的常驻 loop 上。"""
    history = history or []
    visible = tools.visible_tools(ctx)
    schemas = tools.openai_tools(ctx)
    system_prompt = build_system_prompt([t.name for t in visible])

    messages = (
        [{"role": "system", "content": system_prompt}]
        + history
        + [{"role": "user", "content": question}]
    )

    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    denied_writes: set[tuple[str, str]] = set()

    yield _emit(ctx, events.start(ctx))

    try:
        for step in range(1, config.agent_max_steps + 1):
            try:
                resp = await asyncio.wait_for(
                    llm.plan(messages, schemas),
                    timeout=config.agent_step_timeout_s,
                )
            except asyncio.TimeoutError:
                yield _emit(
                    ctx,
                    events.error(
                        ctx, "STEP_TIMEOUT",
                        f"模型第 {step} 步超过 {config.agent_step_timeout_s}s 未响应",
                        retryable=True,
                    ),
                )
                return
            except Exception as exc:
                yield _emit(ctx, events.error(ctx, "MODEL_ERROR", str(exc), retryable=True))
                return

            _add_usage(usage_totals, getattr(resp, "usage", None))
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None) or []

            # ---------------- 没有工具调用：最终回答 ----------------
            if not tool_calls:
                answer_parts: list[str] = []
                stream_usage: dict = {}
                try:
                    async for delta in llm.stream_answer(messages, stream_usage):
                        answer_parts.append(delta)
                        yield _emit(ctx, events.answer(ctx, delta))
                except Exception as exc:
                    yield _emit(ctx, events.error(ctx, "MODEL_ERROR", f"生成回答失败：{exc}"))
                    return
                _add_usage(usage_totals, stream_usage.get("usage"))

                final = "".join(answer_parts).strip()
                if not final:
                    yield _emit(ctx, events.error(ctx, "EMPTY_ANSWER", "模型没有返回回答"))
                    return

                try:
                    memory.append_turn(ctx.session_id, question, final)
                except Exception:
                    # 记忆是旁路：写失败不能把用户的回答带崩
                    pass

                yield _emit(
                    ctx,
                    events.done(
                        ctx,
                        step,
                        usage_totals["prompt_tokens"] or None,
                        usage_totals["completion_tokens"] or None,
                    ),
                )
                return

            # ---------------- 有工具调用 ----------------
            if msg.content:
                yield _emit(ctx, events.thought(ctx, step, msg.content))

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, TypeError):
                    args = {}

                started = time.monotonic()
                yield _emit(ctx, events.tool_call(ctx, step, tc.id, name, args))

                tool = tools.get(name)
                write_key = _write_key(name, args) if tool is not None and tool.side_effect else None
                if tool is None:
                    result = tools.ToolResult(
                        False, f"[工具不存在] {name}", error="unknown_tool"
                    )
                elif tool.side_effect and not ctx.allow_write:
                    result = tools.ToolResult(
                        False, f"[拒绝] 写操作未启用：{name}", error="write_disabled"
                    )
                elif write_key in denied_writes:
                    # 用户已经拒绝过完全相同的写操作。不要再弹一次确认框，
                    # 也不要偷偷执行；把「不要重试」明确告诉模型。
                    result = tools.ToolResult(
                        False,
                        f"[未执行] 用户已拒绝 {name} 的相同操作，不要重试。"
                        "如果确需执行，请让用户重新发起请求。",
                        error="not_confirmed",
                    )
                elif tool.side_effect and ctx.require_confirm:
                    # 先做只读预检：文件根本不存在时不该弹确认框让用户
                    # 批准一个注定失败的操作。
                    pre = await _precheck(tool, args, ctx)
                    if pre is not None:
                        result = pre
                    else:
                        pending = confirm.create(name, args, config.agent_confirm_ttl_s)
                        yield _emit(
                            ctx,
                            events.confirm_required(
                                ctx, tc.id, name, args,
                                pending.token, pending.expires_at,
                            ),
                        )
                        approved = await confirm.wait(pending)
                        if not approved:
                            denied_writes.add(write_key)
                            result = tools.ToolResult(
                                False,
                                f"[未确认] 用户未批准写操作 {name}（拒绝或已超时）",
                                error="not_confirmed",
                            )
                        else:
                            result = await _run_tool(tool, args, ctx)
                else:
                    result = await _run_tool(tool, args, ctx)

                elapsed_ms = int((time.monotonic() - started) * 1000)
                yield _emit(
                    ctx,
                    events.tool_result(ctx, step, tc.id, result, elapsed_ms=elapsed_ms),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.summary,
                    }
                )

        yield _emit(
            ctx,
            events.error(
                ctx, "MAX_STEPS",
                f"超过最大步数 {config.agent_max_steps}，未能得出结论",
            ),
        )

    except asyncio.CancelledError:
        yield _emit(ctx, events.error(ctx, "CANCELLED", "请求已取消"))
        raise


async def _precheck(tool, args: dict, ctx) -> tools.ToolResult | None:
    """预检失败时给出和正式执行同样的错误形态；预检自身异常不阻断确认流程。"""
    try:
        result = await asyncio.wait_for(
            tool.precheck(args, ctx), timeout=tool.timeout_s
        )
    except Exception:
        return None
    return result if isinstance(result, tools.ToolResult) else None


async def _run_tool(tool, args: dict, ctx) -> tools.ToolResult:
    """工具异常一律回灌成 observation，绝不让整个请求 500。"""
    try:
        result = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_s)
        if not isinstance(result, tools.ToolResult):
            return tools.ToolResult(False, f"[工具返回值非法] {tool.name}", error="bad_tool")
        return result
    except asyncio.TimeoutError:
        return tools.ToolResult(
            False,
            f"[工具超时] {tool.name} 超过 {tool.timeout_s}s",
            error="timeout",
        )
    except Exception as exc:
        return tools.ToolResult(
            False,
            f"[工具异常] {tool.name}: {exc}",
            error="tool_error",
        )
