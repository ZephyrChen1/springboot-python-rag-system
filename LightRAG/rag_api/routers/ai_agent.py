"""Agent 问答接口。

与 /ai/chat 并存：新链路独立，旧链路随时可回滚，也便于 A/B 对比。

上游返回 application/x-ndjson：一行一个事件 JSON，解析成本比 SSE 低，
也最贴合 Java 侧已有的字节流 + 行缓冲解码器。
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import config_data as config
from agent import runtime
from agent import events as agent_events
from agent.confirm import resolve as resolve_confirm
from lightrag_service import get_lightrag_service

router = APIRouter(prefix="/ai", tags=["agent"])


class AgentChatRequest(BaseModel):
    session_id: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9_-]{1,128}$",
        description="会话 ID（仅允许字母/数字/下划线/连字符）",
    )
    message: str = Field(..., min_length=1, max_length=20000, description="用户问题")


class AgentConfirmRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=64, description="confirm_required 事件里的 token")
    approved: bool = Field(default=True, description="是否批准该写操作")


@router.post("/agent/chat")
def agent_chat(body: AgentChatRequest, request: Request):
    # 身份只从 BFF 注入的请求头读取。请求体里的 user_id 一律不认。
    raw_user_id = request.headers.get("X-User-Id", "0")
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        user_id = 0

    # 关闭 agent 时不构造 LightRAG（它有自己的常驻 loop 和向量库连接），
    # runtime 会在使用 service 之前先发 AGENT_DISABLED 并返回。
    service = get_lightrag_service() if config.agent_enabled else None
    event_dicts = runtime.stream_events(service, body.message, body.session_id, user_id)

    # StreamingResponse 只接受 str/bytes，事件在这里统一序列化成 NDJSON 行。
    def ndjson():
        for event in event_dicts:
            yield agent_events.to_line(event)

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")


@router.post("/agent/confirm")
def agent_confirm(body: AgentConfirmRequest):
    if not resolve_confirm(body.token, body.approved):
        raise HTTPException(status_code=404, detail="确认请求不存在或已过期")
    return {"ok": True, "approved": body.approved}
