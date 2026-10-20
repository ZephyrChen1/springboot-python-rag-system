"""同步门面：把 agent 循环投递到 LightRAGService 的常驻事件循环。

为什么不能在这里 asyncio.run()：
LightRAG 的模块级共享锁绑定到「第一次 acquire 它的 loop」。agent 循环里要调
aquery_data / ainsert / adelete_by_doc_id，必须和 LightRAG 在同一个 loop 上，
否则会触发 "bound to a different event loop"。

结构与 LightRAGService.stream() 同构：后台协程 -> queue.Queue -> 同步生成器。
"""

import asyncio
import queue
import threading
import uuid

import config_data as config
from . import events, memory, trace
from .loop import run_agent
from .tools.base import ToolContext

_END = object()
# 进程内并发上限。多 worker 时实际上限是 worker 数 × 该值，
# 需要全局上限时只能外置（Redis/DB 计数）。
_SEM = threading.BoundedSemaphore(max(1, config.agent_max_concurrent_runs))


def stream_events(service, question: str, session_id: str, user_id: int = 0):
    """同步生成器，逐条产出事件字典。供 FastAPI 的 StreamingResponse 使用。"""
    run_id = uuid.uuid4().hex
    ctx = ToolContext(
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        lightrag=service,
        allow_write=config.agent_allow_write_tools,
        require_confirm=config.agent_require_confirm,
        trace=trace.TraceWriter(run_id),
    )

    if not config.agent_enabled:
        yield events.error(ctx, "AGENT_DISABLED", "Agent 功能未启用（AGENT_ENABLED=0）")
        return

    # 限制并发 run：单 worker 下多步 agent 会互相拖慢，也容易打满模型限流
    if not _SEM.acquire(blocking=False):
        yield events.error(
            ctx, "BUSY",
            "并发 agent 请求过多，请稍后重试",
            retryable=True,
        )
        return

    try:
        try:
            history = memory.load(session_id)
        except ValueError as exc:
            yield events.error(ctx, "INVALID_SESSION", str(exc))
            return

        q: queue.Queue = queue.Queue()
        cancel = threading.Event()
        # 消费者线程拿不到 loop 上的 Task 对象，由协程自己登记进来。
        task_holder: dict = {}

        async def _run():
            task_holder["task"] = asyncio.current_task()
            agent_gen = run_agent(question, ctx, history)
            try:
                async for event in agent_gen:
                    if cancel.is_set():
                        break
                    q.put(event)
            except BaseException as exc:  # 把异常搬回调用线程抛出
                q.put(exc)
            finally:
                # break 不会自动关闭异步生成器；不显式 aclose 会在 loop 上
                # 留下 pending 的 async_generator_athrow（尤其是断连取消时）。
                aclose = getattr(agent_gen, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except BaseException:
                        pass
                q.put(_END)

        try:
            future = service.submit(_run)
        except Exception as exc:
            yield events.error(ctx, "INTERNAL", f"无法提交到 RAG 事件循环：{exc}")
            return

        try:
            while True:
                item = q.get()
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            cancel.set()
            task = task_holder.get("task")
            cancel_task = getattr(service, "cancel_task", None)
            if task is not None and not task.done() and callable(cancel_task):
                # Future.cancel() 对已经开始执行的协程永远返回 False。
                # 真正的取消必须把 CancelledError 注入到 loop 上的 Task，
                # 这样卡在 LLM 调用 / confirm.wait / 工具里的 await 才会立即退出。
                try:
                    cancel_task(task)
                except Exception:
                    pass
            elif not future.done():
                # 协程还没开始执行（或服务没提供 cancel_task）时的兜底
                future.cancel()
    finally:
        _SEM.release()
