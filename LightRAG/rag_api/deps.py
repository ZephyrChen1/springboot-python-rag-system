#依赖注入
from rag import RagService

def get_rag_service() -> RagService:
    return RagService()

def build_session_config(session_id) -> dict:
    return {"configurable": {"session_id": session_id}}
