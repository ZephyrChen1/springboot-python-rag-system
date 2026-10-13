
# 启动（必须单 worker）：
# uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1
import asyncio
from fastapi import FastAPI, Request
from .routers import kb, ai_chat
import config_data as config
from fastapi.responses import JSONResponse

# 无需鉴权的开放路径（存活探针 + 交互文档）
_OPEN_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

app = FastAPI(title="RAG API", version="1.0.0",)
app.include_router(kb.router)
app.include_router(ai_chat.router)

@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    if config.rag_api_key and request.url.path not in _OPEN_PATHS:
        if request.headers.get("X-API-Key") != config.rag_api_key:
            return JSONResponse({"detail": "无效的 X-API-Key"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}
