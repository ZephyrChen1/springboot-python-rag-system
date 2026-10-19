
import asyncio
import threading
from lightrag import LightRAG, QueryParam
from lightrag.llm.openai import openai_complete_if_cache,openai_embed  #大模型
from lightrag.utils import EmbeddingFunc
import config_data as config
import numpy as np
from concurrent.futures import Future
import os
import queue

#没有结果时的展示消息
_QUERY_NONE_MSG = (
    "LLM 查询返回空结果（很可能是提示词超过模型上下文上限，服务端日志里有 "
    "'maximum context length' 报错）。建议：降低 LIGHTRAG_TOP_K、截断会话历史"
    "（LIGHTRAG_MAX_HISTORY_TURNS），或把云端 vLLM 的 --max-model-len 调大。"
)

# 流式结束哨兵
_END = object()

async def _llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
    return await openai_complete_if_cache(
        config.LLM_MODEL,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        base_url=config.BASE_URL,
        api_key=config.API_KEY,
        stream=kwargs.get("stream"),
    )


async def _openai_compatible_embed(texts,context = None, **kwargs):
    return await openai_embed.func(
        texts,
        model=config.EMBED_MODEL,
        base_url=config.BASE_URL,
        api_key=config.API_KEY,
        max_token_size=config.max_token_embed,
    )



class LightRAGService:
    """同步门面：内部持有一个常驻 loop + 单个 LightRAG 实例。"""

    def __init__(self, working_dir: str | None = None, language: str | None = None):
        self._working_dir = working_dir or config.lightrag_working_dir
        self._language = language or config.lightrag_language
        self._closed = False

        # 常驻事件循环线程（daemon，进程退出即消亡）
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="lightrag-loop", daemon=True
        )
        self._thread.start()

        # 向量存储后端：qdrant
        vector_storage = self._pick_vector_storage()

        self._rag = LightRAG(
            working_dir=self._working_dir,
            llm_model_func=_llm_model_func,
            llm_model_name=config.LLM_MODEL,

            embedding_func=EmbeddingFunc(
                embedding_dim=config.EMBED_DIM,
                max_token_size=2048,
                func=_openai_compatible_embed,
                model_name=config.EMBED_MODEL,  # Qdrant 用其生成 collection 名后缀
            ),

            vector_storage=vector_storage,
            addon_params={"language": self._language},
        )

        # LightRAG 的模块级共享锁(_data_init_lock/_internal_lock)会绑定到
        # "第一次 acquire 它的 event loop"。若在构造/导入期间被别的(临时)loop
        # 抢先绑定，在本服务常驻 loop 上 initialize_storages 就会抛
        # "bound to a different event loop"。故初始化前把锁重建为全新对象，
        # 强制它们在本服务的常驻 loop 上完成首次绑定。
        import lightrag.kg.shared_storage as _shared_storage

        async def _rebind_shared_locks():
            _shared_storage._data_init_lock = asyncio.Lock()
            _shared_storage._internal_lock = asyncio.Lock()
        #以上决定了后面的insert_text是否可行

        self._call(_rebind_shared_locks)
        self._call(self._rag.initialize_storages)

    @staticmethod
    def _pick_vector_storage():
        backend = config.lightrag_vector_backend #默认qdrant

        # QdrantVectorDBStorage 从 os.environ 读连接信息，这里从配置显式注入
        os.environ.setdefault("QDRANT_URL", config.qdrant_url)
        if config.qdrant_api_key:
            os.environ.setdefault("QDRANT_API_KEY", config.qdrant_api_key)
        # workspace 决定数据隔离分区，必须是单一路径组件（不能含 / \）
        # 默认所有实例共享 "_"；这里用工作目录名派生，避免多项目数据混用
        workspace = os.path.basename(
            config.lightrag_working_dir.rstrip("/\\")
        ) or "_"
        os.environ.setdefault("QDRANT_WORKSPACE", workspace)
        # 传字符串类名，由 LightRAG get_storage_class() 解析为实际存储类
        return "QdrantVectorDBStorage"

# ---------------- 内部：事件循环与投递 ----------------
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro_factory, *args, timeout=None, **kwargs):
        """在常驻 loop 上执行协程并阻塞等待结果。"""
        fut: Future = asyncio.run_coroutine_threadsafe(
            coro_factory(*args, **kwargs), self._loop
        )
        return fut.result(timeout=timeout)

    # ---------------- 对外同步接口 ----------------
    def insert_text(self, text: str, file_path: str | None = None) -> str:
        """入库一段文本，返回 track_id。file_path 用于回答时的引用溯源。"""
        return self._call(
            self._rag.ainsert, #将rag.ainsert这个事件送入一个新线程的loop手里
            text,
            file_paths=[file_path] if file_path else None,
        )

    # ---------------- Agent 层使用的接口 ----------------
    def submit(self, coro_factory, *args, **kwargs):
        """把任意协程投递到常驻 loop，返回 concurrent.futures.Future。

        agent 循环必须跑在这个 loop 上：LightRAG 的模块级共享锁绑定在它上面，
        换 loop 会触发 "bound to a different event loop"。
        """
        return asyncio.run_coroutine_threadsafe(
            coro_factory(*args, **kwargs), self._loop
        )

    def cancel_task(self, task: asyncio.Task) -> None:
        """从任意线程取消常驻 loop 上的 Task（线程安全）。

        asyncio.Task.cancel() 不是线程安全的，必须通过 call_soon_threadsafe
        投递到 loop 线程执行。消费者断连时由 runtime 调用。
        """
        if task is None or task.done() or self._closed:
            return
        self._loop.call_soon_threadsafe(task.cancel)

    async def aquery_data(self, query: str, param: QueryParam | None = None):
        """检索但不生成，返回结构化结果。必须在常驻 loop 上调用。

        与 aquery(only_need_context=True) 的区别：后者返回拼好的提示词字符串，
        前者返回 entities / relationships / chunks，便于摘要、截断和引用溯源。
        """
        return await self._rag.aquery_data(query, param=param or QueryParam())

    async def ainsert(self, text: str, file_path: str | None = None) -> str:
        """异步入库。必须在常驻 loop 上调用。"""
        return await self._rag.ainsert(
            text,
            file_paths=[file_path] if file_path else None,
        )

    async def adelete_by_doc_id(self, doc_id: str):
        """异步删文档及其派生数据。必须在常驻 loop 上调用。

        注意：LightRAG 会重建受影响的实体/关系，可能耗时数分钟。
        """
        return await self._rag.adelete_by_doc_id(doc_id)

    def delete_by_doc_id(self, doc_id: str):
        """同步删除（供 agent 工具放到线程池里跑）。

        删除一旦开始就不应该被请求取消打断：半截删除会留下孤立实体/关系。
        工具层用 asyncio.to_thread 调它，这样断连只会停止“等待”，不会
        取消正在进行的删除本身。
        """
        return self._call(self._rag.adelete_by_doc_id, doc_id)

    def stream(
            self,
            question: str,
            conversation_history: list[dict] | None = None,
            mode: str | None = None,
            top_k: int | None = None,
            system_prompt: str | None = None,
            include_references: bool | None = None,
    ):
        def gen():
            q: queue.Queue = queue.Queue()          # 无界：put 永不阻塞，配合 cancel 退出
            cancel = threading.Event()

            async def _run():
                result = None
                try:
                    param = self._build_param(
                        mode=mode,
                        top_k=top_k,
                        include_references=include_references,
                        conversation_history=conversation_history or [],
                        stream=True,
                    )

                    result = await self._rag.aquery(
                        question, param=param, system_prompt=system_prompt
                    )

                    if result is None:
                        raise RuntimeError(_QUERY_NONE_MSG)
                    # 兼容：未真正流式时 aquery 返回 str，直接整段投递
                    if isinstance(result, str):
                        q.put(result) #如果非流式
                    else:
                        async for chunk in result:
                            if cancel.is_set():  # 消费者已走：停止拉取
                                break
                            q.put(chunk) #流式
                except BaseException as exc:  # 把异常搬回调用线程抛出
                    q.put(exc)
                finally:
                    # 显式关闭底层 async generator，避免悬浮在 loop 上泄漏
                    if result is not None and not isinstance(result, str):
                        close = getattr(result, "aclose", None)
                        if close is not None:
                            try:
                                await close()
                            except Exception:
                                pass
                    q.put(_END)

            asyncio.run_coroutine_threadsafe(_run(), self._loop)
            try:
                while True:
                    item = q.get()
                    if item is _END:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    yield item
            finally:
                cancel.set()  # 消费者退出（含断连/异常），通知后台协程停止

        return gen()

    #关闭RAG实例
    def close(self):
        """释放资源：finalize storages，取消遗留后台任务后停止事件循环。

        幂等：可被显式调用 + atexit 重复调用而不会二次清理。
        进程退出时由 atexit 调用；先取消 worker 任务再停循环，避免
        "Event loop is closed / no running event loop" 关闭噪音。
        finalize / 取消任务均带超时上限，防止某次卡死拖住进程无法退出。
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._call(self._rag.finalize_storages, timeout=5)
        except Exception:
            logger.warning("finalize_storages 失败/超时，忽略", exc_info=True)

        async def _shutdown():
            tasks = [
                t
                for t in asyncio.all_tasks(self._loop)
                if t is not asyncio.current_task()
            ]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            self._call(_shutdown, timeout=5)
        except Exception:
            logger.warning("取消遗留任务超时，强制停止事件循环")
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        except Exception:
            pass

     # ---------------- 内部：QueryParam 构建 ----------------
    def _build_param(
            self,
            mode: str | None = None,
            top_k: int | None = None,
            include_references: bool | None = None,
            conversation_history: list[dict] | None = None,
            stream: bool = False,
    ) -> QueryParam:
        return QueryParam(
            mode=mode or config.lightrag_mode,
            top_k=top_k or config.lightrag_top_k,
            enable_rerank=False,  # TODO: 如需接回 qwen3-rerank，改走 LightRAG rerank_model_func
            include_references=(
                include_references
                if include_references is not None
                else config.lightrag_include_references
            ),
            # 会话历史只保留最近 N 轮：长会话若整段塞进提示词，很快撑爆模型上下文
            conversation_history=(
                                         conversation_history or []
                                 )[-config.lightrag_max_history_turns * 2:],
            stream=stream,
        )



# ---------------- 模块级单例 ----------------
_instance: LightRAGService | None = None
_instance_lock = threading.Lock()


def get_lightrag_service() -> LightRAGService:
    """获取全局共享的 LightRAG 服务（入库/查询共用，避免双实例并发写工作目录）。

    Streamlit 可能在多线程里并行执行脚本，单例检查必须加锁（double-checked
    locking），否则并发构造会出现两个服务/两个 event loop，把共享锁绑到不同
    loop 上，触发 "bound to a different event loop"。
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LightRAGService()
    return _instance