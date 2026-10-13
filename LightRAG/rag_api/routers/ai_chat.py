from fastapi import APIRouter
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from ..deps import get_rag_service, build_session_config


router = APIRouter(prefix = "/ai", tags = ["ai问答"])

class AiChatRequest(BaseModel):

    user_id: int = Field(default=0, description="用户 ID")
    session_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="会话 ID（仅允许字母/数字/下划线/连字符）",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description="用户问题",
    )
    top_k: int | None = Field(
        default=None, ge=1, le=100, description="检索候选数，缺省用服务配置"
    )


@router.post("/chat")
def ai_chat(body: AiChatRequest):

    service = get_rag_service()

    gen = service.stream(
        body.message,
        build_session_config(body.session_id),
    )

    return StreamingResponse(gen, media_type = "text/event-stream") #服务器单向持续向浏览器 / 客户端推送消息 的标准类型
