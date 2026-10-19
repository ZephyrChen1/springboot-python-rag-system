"""外部能力工具：计算、时间、联网搜索。

它们的目的不是「多几个功能」，而是让 agent 学会：
**有些问题不该查知识库。**
"""

import ast
import asyncio
import json
import operator
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import config_data as config
from .base import Tool, ToolContext, ToolResult, register


# ---------------- calculator ----------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_MAX_POW_EXP = 100
_MAX_POW_BASE = 1e6


def _eval_node(node: ast.AST):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_POW_EXP or abs(left) > _MAX_POW_BASE:
                raise ValueError("幂运算参数过大")
        result = _BIN_OPS[type(node.op)](left, right)
        if isinstance(result, complex):
            raise ValueError("结果不是实数")
        return result
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    # 明确拒绝函数调用、属性访问、下标、变量名等
    raise ValueError(f"不支持的表达式节点：{type(node).__name__}")


class CalculatorTool(Tool):
    name = "calculator"
    description = (
        "计算数学表达式。支持 + - * / // % ** 和括号。"
        "涉及数字计算时使用，不要自己心算。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，例如 (23*17+5)/4",
            }
        },
        "required": ["expression"],
    }
    timeout_s = 5.0

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        expression = (args.get("expression") or "").strip()
        if not expression:
            return ToolResult(False, "[参数错误] expression 不能为空", error="bad_args")
        if len(expression) > 200:
            return ToolResult(False, "[参数错误] 表达式过长", error="bad_args")
        try:
            tree = ast.parse(expression, mode="eval")
            value = _eval_node(tree)
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError) as exc:
            return ToolResult(False, f"[计算失败] {exc}", error="bad_expression")

        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ToolResult(True, f"{expression} = {value}", data={"result": value})


# ---------------- now ----------------

class NowTool(Tool):
    name = "now"
    description = (
        "获取当前日期和时间。用户问「现在几点」「今天几号」时使用。"
        "不要用知识库或猜测来回答时间问题。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA 时区名，例如 Asia/Shanghai。默认 Asia/Shanghai",
            }
        },
        "required": [],
    }
    timeout_s = 5.0

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        tz_name = (args.get("timezone") or "Asia/Shanghai").strip()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz_name = "Asia/Shanghai"
            tz = ZoneInfo(tz_name)
        current = datetime.now(tz)
        text = current.strftime("%Y-%m-%d %H:%M:%S")
        weekday = "一二三四五六日"[current.weekday()]
        return ToolResult(
            True,
            f"当前时间（{tz_name}）：{text}，星期{weekday}",
            data={
                "timezone": tz_name,
                "iso": current.isoformat(timespec="seconds"),
                "weekday": weekday,
            },
        )


# ---------------- web_search ----------------

class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "联网搜索公开信息。仅在知识库没有覆盖、且用户明确需要外部信息时使用。"
        "优先使用 kb_search；不要用它查公司内部资料。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
            },
        },
        "required": ["query"],
    }
    timeout_s = 20.0

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(False, "[参数错误] query 不能为空", error="bad_args")
        top_n = max(1, min(int(args.get("top_n") or 5), 10))

        def _fetch() -> dict:
            params = urllib.parse.urlencode({"q": query, "n": top_n})
            url = f"{self.endpoint}{'&' if '?' in self.endpoint else '?'}{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "LightRAG-Agent/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            payload = await asyncio.to_thread(_fetch)
        except Exception as exc:
            return ToolResult(False, f"[搜索失败] {exc}", error="web_search_failed")

        results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(results, list):
            return ToolResult(False, "[搜索失败] 返回格式不支持", error="web_search_failed")
        results = results[:top_n]

        lines = [f"搜索「{query}」得到 {len(results)} 条结果："]
        for i, item in enumerate(results, 1):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or "(无标题)"
            snippet = (item.get("snippet") or item.get("content") or "")[:300]
            link = item.get("url") or ""
            lines.append(f"{i}. {title}\n   {snippet}\n   {link}")

        return ToolResult(
            True,
            "\n".join(lines)[: config.agent_tool_result_max_chars],
            data={"query": query, "results": results},
        )


register(CalculatorTool())
register(NowTool())

# 没有配置 provider 就不注册：宁可模型看不到，也不要给它一个永远失败的工具。
if config.agent_web_search_enabled and config.agent_web_search_url:
    register(WebSearchTool(config.agent_web_search_url))
