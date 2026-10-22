# LightRAG 知识库服务

基于 [LightRAG](https://github.com/HKUDS/LightRAG) 的知识库检索与问答服务，作为 AI 知识智能体的
**Python 上游**，向 Java BFF 提供 HTTP 接口。

服务包含四条链路：

- **入库**：上传文档 → 解析为文本 → LightRAG 抽取实体/关系 → 写入 Qdrant 与本地图索引；
- **固定问答**：结合会话历史做检索增强生成，SSE 流式返回；
- **Agent 问答**：ReAct 工具循环，模型自主决定检索模式、次数与工具调用；
- **抓取动态**：只读暴露 `news_ingest` 的 SQLite 状态，供 Web 展示。

## 功能

- 文档入库：MD5 去重、后台线程执行、可轮询真实进度、失败可重试；
- 两套问答链路并存：`/ai/chat` 与 `/ai/agent/chat` 独立部署、可灰度回滚；
- Agent 工具：知识库检索、入库状态、计算、时间、业务查询、可选联网搜索；
- 写操作人类在环：写工具默认隐藏，开启后仍需人工确认；
- 会话历史落盘，按轮数与单条长度截断，避免撑爆模型上下文；
- API 鉴权（可选的 `X-API-Key`）；
- 独立采集进程 `news_ingest`，不导入本服务、只经 HTTP 入库；
- Streamlit 管理/体验页面（上传、问答各一个，历史遗留，可不用）。

## 目录结构

```text
.
├── config_data.py            # 全局配置（模型、向量库、分块、Agent、入库任务）
├── lightrag_service.py       # LightRAG 封装：常驻事件循环 + 全局单例
├── knowledge_base.py         # 入库服务：MD5 去重 + 终态判定 + 进度读取
├── document_loader.py        # 文档解析为纯文本
├── file_history_store.py     # 会话历史文件存储
├── rag.py                    # 固定问答：检索 + 历史 + 流式生成
├── agent/                    # Agentic RAG 运行时
│   ├── loop.py               # ReAct 循环（最多 AGENT_MAX_STEPS 步）
│   ├── prompt.py             # system prompt（含提示注入防护）
│   ├── runtime.py            # 提交到 LightRAG 常驻事件循环、并发上限、取消
│   ├── events.py             # NDJSON 事件协议
│   ├── confirm.py            # 写操作确认 token
│   ├── memory.py             # 会话历史
│   ├── trace.py              # agent_runs/ 轨迹
│   ├── llm.py                # 模型调用
│   └── tools/                # kb / biz / external 工具
├── rag_api/                  # FastAPI 服务
│   ├── main.py               # 应用入口 + 鉴权中间件
│   ├── ingest.py             # 入库调度（跨进程写锁 + 单 worker）
│   ├── jobs.py               # 入库任务注册表（SQLite，跨进程可查）
│   ├── staging.py            # Agent 文件暂存区
│   ├── news_store.py         # news.db 只读查询
│   └── routers/
│       ├── kb.py             # 知识库接口
│       ├── ai_chat.py        # 固定问答
│       ├── ai_agent.py       # Agent 问答与确认
│       └── news.py           # 抓取动态只读接口
├── news_ingest/              # 独立采集进程（详见其 README）
├── tests/                    # 107 个单元测试
├── docs/                     # Agent 改造说明
├── streamlit_file_uploader.py
├── streamlit_qa.py
├── requirements.txt
└── .env.example
```

运行时生成、**不纳入版本控制**的目录：`Lightrag_db/`（向量索引与 doc_status）、
`chat_history/`（会话记录，含 `.locks/`）、`crawler_state/`（新闻状态库、入库任务库
`rag_jobs.db` 与写锁）、`uploads/`（Agent 暂存区）、`agent_runs/`（Agent 轨迹）、
`md5.text`（入库指纹）。

## 环境要求

- Python 3.10+（开发环境为 3.13）
- 一个 Qdrant 实例（本地 Docker 或 Qdrant Cloud）
- 一个 OpenAI 兼容的大模型与向量模型服务（默认 DashScope）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置密钥

```bash
cp .env.example .env
```

编辑 `.env` 填入 `DASHSCOPE_API_KEY`（或改用其他兼容 OpenAI 协议的服务，同时调整
`DASHSCOPE_BASE_URL`）。`.env` 已被 `.gitignore` 忽略，**切勿提交**。也可改用系统环境变量。

> `load_dotenv()` 默认不覆盖已存在的环境变量。如果终端里已经 export 过
> `DASHSCOPE_API_KEY`，它会优先于 `.env`；排查 401/欠费时先确认用的是哪一份。

### 3. 启动 Qdrant

在仓库根目录用 compose 起（推荐，数据在 named volume 里）：

```bash
docker compose up -d
```

或者手工起一个临时容器：

```bash
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant
```

云服务则在 `.env` 中填写 `QDRANT_URL` 与 `QDRANT_API_KEY`。

### 4. 启动 API 服务

```bash
uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1
```

> **仍然必须单 worker。** 入库任务状态（`crawler_state/rag_jobs.db`）和 KB 写锁
> （`crawler_state/ingest.lock`）已经跨进程，重启不丢 job、两个进程也不会同时写；
> 但 LightRAG 实例仍是进程内单例，其 KV/doc_status 是文件型存储、每个进程各持一份
> 内存快照，多 worker 会互相覆盖。为什么 A 路线还不够、后续怎么扩容见
> [扩容路线](#扩容路线)。

交互文档：<http://localhost:8102/docs>

### 5. 启动 Streamlit 页面（可选、历史遗留）

```bash
streamlit run streamlit_file_uploader.py   # 知识库更新
streamlit run streamlit_qa.py              # 固定问答
```

Web 主界面在 `computer-biz-service/frontend`，这两个页面只用于本地快速验证。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 存活探针 |
| `POST` | `/v1/kb/upload` | 上传文档并入库，`multipart/form-data`，字段 `file`；返回 `job_id` |
| `POST` | `/v1/kb/stage` | 只暂存文件，返回 `staged_id`，供 Agent 入库使用 |
| `GET` | `/v1/kb/ingest/{job_id}` | 轮询入库进度（已完成任务保留 1 小时，最多 20 个） |
| `POST` | `/ai/chat` | 固定链路流式问答，`text/event-stream` |
| `POST` | `/ai/agent/chat` | Agent 事件流，`application/x-ndjson` |
| `POST` | `/ai/agent/confirm` | 批准 / 拒绝一次等待中的写操作 |
| `GET` | `/v1/news/overview` | 抓取统计、源状态、最近运行（只读） |
| `GET` | `/v1/news/items` | 抓取文章分页列表（只读） |
| `GET` | `/v1/news/items/{item_id}` | 抓取文章详情（只读） |

固定问答请求体：

```json
{
  "user_id": 1,
  "session_id": "user_001",
  "message": "你好",
  "top_k": 20
}
```

`session_id` 仅允许字母、数字、下划线、连字符（长度 1–128）。

Agent 请求体：

```json
{
  "session_id": "user_001",
  "message": "把 doc01.txt 入库"
}
```

Agent 身份只从请求头 `X-User-Id` 读取，请求体里的 `user_id` 一律不认。

### 鉴权

设置 `RAG_API_KEY` 后，除 `/health`、`/docs`、`/redoc`、`/openapi.json` 外的接口都需携带：

```text
X-API-Key: <RAG_API_KEY>
```

留空则不启用鉴权，**仅限内网/开发环境**。Java BFF 与服务端共享同一密钥。

## Agentic RAG

`/ai/agent/chat` 是一个最多 `AGENT_MAX_STEPS`（默认 6）步的 ReAct 循环：

1. 模型看到用户问题与当前可用工具；
2. 需要外部信息时发起 `tool_call`；
3. 工具结果作为观察回灌；失败也回灌，模型可以换策略而不是整条请求失败；
4. 信息足够时输出答案，或达到步数上限返回 `MAX_STEPS`。

### 工具

| 工具 | 作用 | 默认可见 |
|---|---|---|
| `kb_search` | 知识库检索，可选 `local`/`global`/`hybrid`/`naive`/`mix` 与 `top_k` | ✅ |
| `kb_status` | 按 `job_id` / `file_name` 查入库进度、doc_status 或暂存区 | ✅ |
| `calculator` | 受限数学表达式计算 | ✅ |
| `now` | 当前时间（默认 `Asia/Shanghai`） | ✅ |
| `web_search` | 外部搜索 | 仅配置 provider 后 |
| `biz_query` | 会话 / 消息 / 入库任务查询 | 仅配置 `AGENT_BIZ_QUERY_KEY` 后 |
| `kb_ingest` | 把暂存文件入库 | 仅 `AGENT_ALLOW_WRITE_TOOLS=1` |
| `kb_delete` | 删除知识库文档 | 仅 `AGENT_ALLOW_WRITE_TOOLS=1` |

`biz_query` 不直连数据库：它回调 Java 的 `/internal/rag/query`，只接受白名单查询
（`user_sessions` / `session_messages` / `ingest_tasks`）与参数，SQL 与身份由 Java 注入。

### 事件协议

上游按行输出 NDJSON，每行一个事件：

| 事件 | 内容 |
|---|---|
| `start` | `run_id`、`session_id` |
| `thought` | 模型在工具调用前的说明 |
| `tool_call` | `call_id`、`name`、`args` |
| `tool_result` | `ok`、`summary`、`data`、`elapsed_ms`、可选 `error` |
| `answer` | 正文增量 `delta` |
| `confirm_required` | 写操作确认 `token`、工具名、参数、过期时间 |
| `done` | `steps`、`usage`、`finish_reason` |
| `error` | `code`、`message`、`retryable` |

**只有 `done` 代表成功。** `BUSY`、`MODEL_ERROR`、`MAX_STEPS` 都是先发 `error`
再正常结束流，HTTP 200 与「流正常关闭」都不等于成功。Java 与前端各自独立判断终态。

### 写操作确认

- `kb_ingest` / `kb_delete` 在 `AGENT_ALLOW_WRITE_TOOLS=0` 时不会出现在模型可见工具列表；
- 开启后，执行前先做预检，再发 `confirm_required` 并等待；
- 前端调用 `POST /ai/agent/confirm` 批准或拒绝，确认 token 默认 120 秒过期；
- 拒绝后工具不执行，Agent 继续给出最终回答；
- 文件正文只存在于暂存区，模型只拿得到 `staged_id` 或文件名。

## 新闻采集

`news_ingest` 是独立进程，不导入本服务，也不直接写 `Lightrag_db/`、`uploads/` 或
`md5.text`；所有入库都经运行中的 FastAPI：

```bash
python -m news_ingest --check-sources   # 只检查源
python -m news_ingest --once            # 跑一轮
python -m news_ingest --loop            # 常驻
```

支持 RSS 2.0 / RSS 1.0 / Atom 与 sitemap；默认启用 8 个官方 AI 源。完整参数、过滤规则、
状态机、重试与定时任务见 [news_ingest/README.md](news_ingest/README.md)。

Web 侧只读接口 `/v1/news/*` 直接查 `crawler_state/news.db`，不触发抓取、不写库。

## 入库语义

- 上传内容先算 MD5，命中则直接返回 `[跳过]`；
- 未命中时提交给 LightRAG，并等待该文件在 `doc_status` 出现终态；
- **只有 `processed` 才写 MD5 指纹**；`failed` 与状态未确认都返回 `[失败]`，不写指纹；
- 这样修复了「实体抽取失败仍返回任务号、却被误记为成功，导致内容永远无法重试」的问题；
- 入库与删除共用一把跨进程写锁（`crawler_state/ingest.lock`），任何时刻只有单写者；
- 入库任务放 SQLite（WAL），API 重启或多进程查询不会丢 job。

## 配置说明

配置集中在 `config_data.py`，均支持环境变量覆盖，常用项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | 大模型/向量模型密钥（必填） |
| `DASHSCOPE_BASE_URL` | DashScope 兼容接口 | 可替换为其他兼容服务 |
| `LLM_MODEL` | `qwen-max` | 生成模型 |
| `QDRANT_URL` / `QDRANT_API_KEY` | `http://localhost:6333` / 空 | 向量库连接 |
| `RAG_API_KEY` | 空 | 服务鉴权密钥 |
| `LIGHTRAG_CHUNK_TOKEN_SIZE` | `2400` | 每块最大 token，越大入库越快 |
| `LIGHTRAG_TOP_K` | `20` | 检索候选数；知识库内容多时建议 15–20 |
| `LIGHTRAG_MAX_HISTORY_TURNS` | `10` | 会话历史保留轮数 |
| `LIGHTRAG_MODE` | `hybrid` | 检索模式：`naive`/`local`/`global`/`hybrid`/`mix` |
| `INGEST_JOB_DB` | `./crawler_state/rag_jobs.db` | 入库任务状态库 |
| `INGEST_LOCK_PATH` | `./crawler_state/ingest.lock` | 入库/删除共用的写锁 |
| `KB_DELETE_LOCK_TIMEOUT_S` | `30` | 删除时等待写锁的最长秒数 |
| `AGENT_ENABLED` | `1` | 关闭后返回结构化 `AGENT_DISABLED` |
| `AGENT_MAX_STEPS` | `6` | 决策循环步数上限 |
| `AGENT_STEP_TIMEOUT_S` | `90` | 单步超时 |
| `AGENT_MAX_CONCURRENT_RUNS` | `2` | 并发 Agent 运行上限 |
| `AGENT_ALLOW_WRITE_TOOLS` | `0` | 写工具总闸门 |
| `AGENT_REQUIRE_CONFIRM` | `1` | 写操作人工确认 |
| `AGENT_CONFIRM_TTL_S` | `120` | 确认 token 有效期 |
| `AGENT_BIZ_QUERY_URL` / `AGENT_BIZ_QUERY_KEY` | Java 内部接口 / 空 | 未配置 key 时不注册 `biz_query` |

支持的文件类型见 `config_data.py` 的 `supported_file_extensions`（当前仅 `.txt`）。

## 扩容路线

当前「单 worker」不是随便加的保险，而是由 LightRAG 的存储模型决定的。
进程内状态已经拆成三类，处理方式不同：

| 状态 | 原来 | 现在 | 跨进程 |
|---|---|---|---|
| 入库任务 job | `rag_api.ingest._jobs` 字典 | `crawler_state/rag_jobs.db`（SQLite WAL） | ✅ |
| 入库/删除互斥 | `threading.Lock` | `crawler_state/ingest.lock`（OS 文件锁 + 进程内线程锁） | ✅ |
| 会话历史 | 整文件读-改-写 | 按 session 的文件锁串行 | ✅ |
| LightRAG KV / doc_status | 文件型 JSON，进程内快照 | 未变 | ❌ |

所以重启 API 不会丢 job，crawler、agent 删除和入库也不会同时写；但开
`--workers N` 仍然不安全：每个进程会各自把 `kv_store_*.json` 读进内存，
写入时整体覆盖，N 个进程轮流出题就会丢数据。

按投入从低到高：

1. **A（已完成）**：任务状态、写锁、会话历史外置。解决「重启丢 job」和「并发写互踩」。
2. **B**：继续单写进程，多读进程。写进程独占 LightRAG；读进程只读 Qdrant +
   共享 KV（把 `JsonKVStorage` 换成 Redis/Postgres 实现）。
3. **C**：入库改成异步队列（API 只入队，单独 worker 消费），读写彻底分离。

在 B/C 落地前，请保持 `--workers 1`；单 worker 下的并发保护（agent `_SEM`、入库互斥）
都是进程内的，多 worker 时上限会变成「worker 数 × 各自上限」。

## 测试

```bash
python -m unittest discover -s tests -t .
```

当前 107 个用例，覆盖 Agent 循环、入库任务、doc_status 终态判定、新闻流水线与新闻只读接口；
测试使用 fake LLM / 临时目录，不依赖在线模型与真实 Qdrant。

## 注意

- 入库耗时主要来自「切块 + 逐块调 LLM 抽取实体/关系」，属正常现象；进度可通过
  `/v1/kb/ingest/{job_id}` 或 `kb_status` 查看。
- 提问返回空结果时，多为提示词超出模型上下文上限，可调小 `LIGHTRAG_TOP_K`、
  `LIGHTRAG_MAX_HISTORY_TURNS`。
- 模型返回欠费、限流等上游错误时，Agent 会发 `error` 事件而不是 `done`；
  不要用 HTTP 200 判断成功。
