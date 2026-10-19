"""工具协议与注册表。

一个工具 = 名字 + 给模型看的描述 + JSON Schema + 异步实现。
描述（description）是给模型的提示词，要写清「什么时候用、什么时候不用」。
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolContext:
    user_id: int
    session_id: str
    run_id: str
    lightrag: Any                    # LightRAGService
    allow_write: bool = False        # 写工具总闸门
    require_confirm: bool = False    # 写操作是否需要人工确认
    trace: Any = None                # trace.TraceWriter，可为 None


@dataclass
class ToolResult:
    ok: bool
    summary: str                     # 给 LLM 的 observation，长度由 events 层截断
    data: dict | None = None         # 给前端/落库的结构化数据
    error: str | None = None


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict = {}
    timeout_s: float = 30.0
    side_effect: bool = False        # 写操作必须置 True

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    async def precheck(self, args: dict, ctx: ToolContext) -> ToolResult | None:
        """写操作的只读预检，在弹人工确认框**之前**调用。

        返回 ToolResult 表示「这一步注定失败」（文件不存在、参数非法），
        循环会直接把它当作结果，不再打扰用户确认；返回 None 才进入确认流程。
        默认不做检查。
        """
        return None


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if not tool.name:
        raise ValueError(f"{type(tool).__name__} 缺少 name")
    if tool.name in _REGISTRY:
        raise ValueError(f"工具重复注册：{tool.name}")
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def visible_tools(ctx: ToolContext) -> list[Tool]:
    """写工具在 allow_write=False 时对模型完全不可见。"""
    return [
        t for t in _REGISTRY.values()
        if not (t.side_effect and not ctx.allow_write)
    ]


def openai_tools(ctx: ToolContext) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in visible_tools(ctx)
    ]
