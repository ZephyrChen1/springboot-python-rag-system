
# 启动（当前仍必须单 worker）：
# uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1
#
# 任务状态（crawler_state/rag_jobs.db）和 KB 写锁（crawler_state/ingest.lock）
# 已经跨进程，API 重启不会再丢 job；但 LightRAG 实例仍是进程内单例，
# 其 KV/doc_status 是文件型存储、每个进程各持一份内存快照。
# 多 worker 会让不同进程的写互相覆盖，因此在把 KV 换成共享后端之前，
# 关闭多 worker 仍然是硬要求。详见 README「扩容路线」。
import asyncio
from fastapi import FastAPI, Request
from .routers import kb, ai_chat, ai_agent, news
import config_data as config
from fastapi.responses import JSONResponse

# 无需鉴权的开放路径（存活探针 + 交互文档）
_OPEN_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

app = FastAPI(title="RAG API", version="1.0.0",)
app.include_router(kb.router)
app.include_router(ai_chat.router)
app.include_router(news.router)
# 路由始终注册：AGENT_ENABLED=0 时由 runtime 返回结构化的 AGENT_DISABLED 事件，
# 而不是让前端收到一个没有解释的 404。路由内部也不会构造 LightRAG 实例。
app.include_router(ai_agent.router)

@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    if config.rag_api_key and request.url.path not in _OPEN_PATHS:
        if request.headers.get("X-API-Key") != config.rag_api_key:
            return JSONResponse({"detail": "无效的 X-API-Key"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}
