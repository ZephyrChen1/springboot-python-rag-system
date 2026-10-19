"""业务数据查询（Phase 4）。

设计约束：**不给模型自由 SQL**。
Python 侧只发送「预定义查询名 + 参数」，SQL 与表名写死在 Java BFF。
这条链路让 Python 运行时回连 Java，是刻意接受的架构代价——
换来「业务库 schema 只在 Java 一处」和「MySQL 凭证不下发到 Python」。
"""

import asyncio
import json
import urllib.request

import config_data as config
from .base import Tool, ToolContext, ToolResult, register

# 查询名 -> 模型**可以**传的参数键。Java 侧必须实现同名查询。
#
# ★ 身份类参数（user_id / session_id）不在这里：它们由服务端从 ToolContext 注入。
#   让模型自己填 user_id，等于把「查谁的会话」交给一段可能被提示注入的文本，
#   模型随手传个 2 就是越权。模型能控制的只有 limit。
_QUERIES = {
    "user_sessions": {"limit"},
    "session_messages": {"limit"},
    "ingest_tasks": {"limit"},
}

# 查询名 -> 服务端注入的身份参数
_SCOPED = {
    "user_sessions": lambda ctx: {"user_id": ctx.user_id},
    "session_messages": lambda ctx: {"user_id": ctx.user_id, "session_id": ctx.session_id},
    "ingest_tasks": lambda ctx: {"user_id": ctx.user_id},
}


class BizQueryTool(Tool):
    name = "biz_query"
    description = (
        "查询业务系统数据（会话列表、当前会话的消息、入库任务）。"
        "只能使用预定义查询：user_sessions / session_messages / ingest_tasks。"
        "用户问「我有哪些会话」「这个会话之前聊了什么」「入库任务怎么样了」时使用。"
        "查询范围自动限定为当前用户（和当前会话），你不需要也不能指定 user_id / session_id。"
        "不支持自由 SQL，也不要尝试构造 SQL。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "enum": list(_QUERIES.keys()),
                "description": "预定义查询名",
            },
            "params": {
                "type": "object",
                "description": (
                    "可传的只有 limit（1~100，默认 20）；"
                    "user_id / session_id 由服务端自动注入，不要传。"
                ),
            },
        },
        "required": ["query"],
    }
    timeout_s = 15.0

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = (args.get("query") or "").strip()
        if query not in _QUERIES:
            return ToolResult(
                False,
                f"[参数错误] 未知查询 {query}，可用：{', '.join(_QUERIES)}",
                error="bad_args",
            )

        raw_params = args.get("params") or {}
        if not isinstance(raw_params, dict):
            return ToolResult(False, "[参数错误] params 必须是对象", error="bad_args")

        allowed = _QUERIES[query]
        unknown = set(raw_params) - allowed
        if unknown:
            return ToolResult(
                False,
                f"[参数错误] {query} 不接受参数：{', '.join(sorted(unknown))}",
                error="bad_args",
            )

        # 限制返回条数，避免工具结果撑爆上下文
        params = dict(raw_params)
        try:
            params["limit"] = max(1, min(int(params.get("limit", 20)), 100))
        except (TypeError, ValueError):
            params["limit"] = 20

        # ★ 身份参数只从这里来。模型传的同名键在上面已被 unknown 检查拒掉。
        params.update(_SCOPED[query](ctx))

        payload = json.dumps({"query": query, "params": params}).encode("utf-8")

        def _post() -> dict:
            req = urllib.request.Request(
                config.agent_biz_query_url,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": config.agent_biz_query_key,
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            result = await asyncio.to_thread(_post)
        except Exception as exc:
            return ToolResult(False, f"[查询失败] {exc}", error="biz_query_failed")

        items = result.get("items") if isinstance(result, dict) else result
        if not isinstance(items, list):
            items = [] if items is None else [items]

        summary = f"查询 {query} 返回 {len(items)} 条记录。\n"
        summary += json.dumps(items[:10], ensure_ascii=False, default=str)[
            : config.agent_tool_result_max_chars - 100
        ]
        return ToolResult(True, summary, data={"query": query, "items": items[:50]})


# 没有配置内部密钥就不注册：Java 侧接口这时还不存在。
if config.agent_biz_query_key:
    register(BizQueryTool())
