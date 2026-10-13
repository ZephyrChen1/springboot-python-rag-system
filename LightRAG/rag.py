import config_data as config
from langchain_core.messages import AIMessage, HumanMessage
from lightrag_service import get_lightrag_service
from file_history_store import get_history

class RagService:
    def __init__(self, enable_tracing: bool = True):
        self.lightrag = get_lightrag_service()

        if enable_tracing:
            pass

    @staticmethod
    def _session_id(session_config: dict | None) -> str:
        return (session_config or {}).get("configurable", {}).get("session_id", "user_001")

    #将Langchain消息转为LightRAG
    @staticmethod
    def _history_to_lr(messages) -> list[dict]:
        """LangChain BaseMessage 列表 -> LightRAG conversation_history 字典列表。"""
        role_map = {"human": "user", "ai": "assistant", "system": "system"}
        out = []
        for m in messages:
            content = m.content
            if not isinstance(content, str):
                content = str(content)
            out.append({
                "role": role_map.get(getattr(m, "type", ""), "user"),  # getattr，得到m的type属性，没有则返回""
                "content": content,
            })
        return out

    #保存历史记录
    def _save_turn(self, session_id: str, question: str, answer: str) -> None:
        get_history(session_id).add_messages([
            HumanMessage(content=question),
            AIMessage(content=answer),
        ])

    def stream(self, question: str, session_config: dict | None = None):
        """流式问答：返回生成器逐块产出回答，流结束后自动保存会话历史。"""
        session_id = self._session_id(session_config)
        history = self._history_to_lr(get_history(session_id).messages)

        def gen():
            pieces = []
            for chunk in self.lightrag.stream(
                question,
                conversation_history=history,
            ):
                pieces.append(chunk)
                yield chunk
            # 流正常结束后再写历史；异常/中断时不写
            self._save_turn(session_id, question, "".join(pieces))

        return gen()

