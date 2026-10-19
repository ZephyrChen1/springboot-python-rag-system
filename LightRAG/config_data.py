import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # 从项目根目录 .env 加载密钥等环境变量（未安装 python-dotenv 则跳过）
except ImportError:
    pass

lightrag_working_dir = "./Lightrag_db"
lightrag_language = os.environ.get("LIGHTRAG_LANGUAGE", "Simplified Chinese")  # 实体/关系抽取语言

md5_path = './md5.text'

#模型参数
BASE_URL = os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
LLM_MODEL = "qwen-plus"
EMBED_MODEL = "text-embedding-v4"
EMBED_DIM = 1024   # text‑embedding‑v4向量维度1024
max_token_embed = 2048
max_token_llm = 8192

#向量存储后端
lightrag_vector_backend = "qdrant"

# ---- Qdrant 连接配置 ----
# 本地 Docker 部署：docker run -d --name qdrant -p 6333:6333 qdrant/qdrant
# 云服务（Qdrant Cloud）：在 Qdrant Cloud 控制台获取 URL 与 API Key
qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
qdrant_api_key = os.environ.get("QDRANT_API_KEY", "")   # 本地无鉴权留空

#支持的拓展名
# supported_file_extensions = [".txt", ".md", ".pdf", ".docx", ".csv", ".xlsx"]
supported_file_extensions = [".txt"] #先测试用

# ==== [RAG 服务化封装] API 服务鉴权 ====
# 配置后（推荐）要求所有请求携带 X-API-Key 头（/health /docs 除外）；
# 留空则不启用鉴权，仅用于内网/开发环境。Java 后台与 RAG 服务共享此密钥。
rag_api_key = os.environ.get("RAG_API_KEY", "")

def validate_config(strict: bool = False) -> None:
    """校验必要配置是否齐全。

    - strict=False：仅打日志警告（供工具脚本/单测调用，缺失也能继续）
    - strict=True ：缺失则抛 ValueError（供应用启动时调用，缺密钥直接失败）
    """
    missing = []
    if not API_KEY:
        missing.append("API_KEY")

    if missing:
        msg = (
            f"缺少环境变量: {', '.join(missing)}。"
            "请在项目根目录 .env 文件（参照 .env.example）中配置，或设置系统环境变量。"
        )
        if strict:
            raise ValueError(msg)
        logging.getLogger(__name__).warning(msg)

# get_history 需要传入 session_id；注意：企业级应支持动态会话 ID
session_config = {
    "configurable": {
        "session_id": "user_001"
    }
}

# ---- LightRAG 入库速度调优 ----
# 入库慢的主因是"切块 + 每个 chunk 反复调 LLM 做实体/关系抽取"。以下参数按"少调用、高并发"调优：
lightrag_chunk_token_size = int(os.environ.get("LIGHTRAG_CHUNK_TOKEN_SIZE", "2400"))
    # 每块最大 token 数。默认 1200；加大→chunk 数变少→LLM 抽取次数变少。
    # 注意：过大单块会让单次抽取上下文变长、单次调用更慢，24 00 是较均衡取值。
lightrag_chunk_overlap_token_size = int(
    os.environ.get("LIGHTRAG_CHUNK_OVERLAP_TOKEN_SIZE", "100")
)   # 相邻 chunk 重叠 token 数，默认 100
lightrag_entity_gleaning = int(
    os.environ.get("LIGHTRAG_ENTITY_GLEANING", "0")
)   # 实体抽取后是否再补一轮(默认 1)。0=关闭，省一轮 LLM 调用；质量略降，可按需开回 1。
lightrag_llm_max_async = int(os.environ.get("LIGHTRAG_LLM_MAX_ASYNC", "8"))
    # LLM 并发数，默认 4。多 chunk 时并行抽取加速；受 DashScope 限流约束，不建议过大。
lightrag_embedding_max_async = int(
    os.environ.get("LIGHTRAG_EMBEDDING_MAX_ASYNC", "16")
)   # embedding 并发数，默认 8。
lightrag_embedding_batch_num = int(
    os.environ.get("LIGHTRAG_EMBEDDING_BATCH_NUM", "10")
)   # 每批 embedding 条数。注意：DashScope 单次硬上限 10，改大无效且会 400 报错，勿动。
lightrag_mode = os.environ.get("LIGHTRAG_MODE", "hybrid")  # naive/local/global/hybrid/mix
# 图检索候选实体/关系数。注意：每轮问答会把检索到的实体/关系/文本块整段拼进提示词，
# top_k 过大 + 知识库内容多时会撑爆模型上下文（qwen2.5-32b 部署上限 32768 token），
# 表现为 LLM 400 且服务端日志报 'maximum context length'。知识库内容多时建议 15-20。
lightrag_top_k = int(os.environ.get("LIGHTRAG_TOP_K", "20"))
# 会话历史只保留最近 N 轮（每轮 2 条消息），防止长会话把历史整段塞进提示词超限。
lightrag_max_history_turns = int(os.environ.get("LIGHTRAG_MAX_HISTORY_TURNS", "10"))
lightrag_include_references = True   # 回答末尾附带引用来源
lightrag_language = os.environ.get("LIGHTRAG_LANGUAGE", "Simplified Chinese")  # 实体/关系抽取语言
lightrag_query_timeout = 600     # 单次查询超时(秒)；入库耗时更长，不设上限

# ==== [Agentic RAG] 决策循环配置 ====
# 详见 docs/Agent化改造实施计划.md
def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


agent_enabled = _env_bool("AGENT_ENABLED", "1")
agent_max_steps = int(os.environ.get("AGENT_MAX_STEPS", "6"))
agent_step_timeout_s = float(os.environ.get("AGENT_STEP_TIMEOUT_S", "90"))
agent_tool_result_max_chars = int(os.environ.get("AGENT_TOOL_RESULT_MAX_CHARS", "2000"))
agent_history_max_turns = int(os.environ.get("AGENT_HISTORY_MAX_TURNS", "10"))
# 单条历史消息回放的最大字符数。长回答/长问题在写入和读取时都会截断，
# 避免 10 轮历史把模型上下文撑爆（旧链路已经因为 top_k 撞过一次上限）。
agent_history_max_chars = int(os.environ.get("AGENT_HISTORY_MAX_CHARS", "2000"))
agent_max_concurrent_runs = int(os.environ.get("AGENT_MAX_CONCURRENT_RUNS", "2"))

# 写工具总闸门：默认关闭。关闭时模型在 tools 列表里看不到 kb_ingest / kb_delete。
agent_allow_write_tools = _env_bool("AGENT_ALLOW_WRITE_TOOLS", "0")
# 写操作人工确认：开启后，写工具执行前会先发 confirm_required 事件等待用户确认。
agent_require_confirm = _env_bool("AGENT_REQUIRE_CONFIRM", "1")
agent_confirm_ttl_s = int(os.environ.get("AGENT_CONFIRM_TTL_S", "120"))

# 外部能力
agent_web_search_enabled = _env_bool("AGENT_WEB_SEARCH_ENABLED", "0")
agent_web_search_url = os.environ.get("AGENT_WEB_SEARCH_URL", "")

# 暂存区与 trace（运行时生成，不纳入版本控制）
agent_stage_dir = os.environ.get("AGENT_STAGE_DIR", "./uploads")
agent_trace_dir = os.environ.get("AGENT_TRACE_DIR", "./agent_runs")
# 暂存文件大小上限（字节）。默认 100MB，与 Spring multipart 的 max-file-size 对齐；
# 没有这个上限，/v1/kb/stage 就是一个可以打满磁盘的匿名入口。
agent_stage_max_bytes = int(
    os.environ.get("AGENT_STAGE_MAX_BYTES", str(100 * 1024 * 1024))
)

# Phase 4：业务数据查询走 Java BFF 的受限接口，不直连 MySQL
agent_biz_query_url = os.environ.get(
    "AGENT_BIZ_QUERY_URL", "http://localhost:8080/internal/rag/query"
)
agent_biz_query_key = os.environ.get("AGENT_BIZ_QUERY_KEY", "")