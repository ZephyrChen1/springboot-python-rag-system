# LightRAG 知识库服务

基于 [LightRAG](https://github.com/HKUDS/LightRAG) 的知识库检索与问答服务，作为全栈项目的 **Python 上游**，向 Java 后台提供 HTTP 接口。

提供两条链路：

- **入库**：上传文档 → 解析为文本 → LightRAG 抽取实体/关系并写入向量库（Qdrant）
- **问答**：结合会话历史的流式检索问答（SSE 流式返回）

## 功能

- 文档入库（MD5 去重、后台线程执行、可轮询真实进度）
- 流式问答（会话历史按轮数截断，避免撑爆模型上下文）
- 会话历史落盘（按 `session_id` 分文件）
- API 鉴权（可选的 `X-API-Key`）
- Streamlit 管理/体验页面（上传、问答各一个）

## 目录结构

```
.
├── config_data.py            # 全局配置（模型、向量库、分块与检索参数）
├── lightrag_service.py       # LightRAG 封装：常驻事件循环 + 全局单例
├── knowledge_base.py         # 入库服务：MD5 去重 + 入库进度读取
├── document_loader.py        # 文档解析为纯文本
├── file_history_store.py     # 会话历史文件存储
├── rag.py                    # RagService：组合检索与历史，产出流式回答
├── rag_api/                  # FastAPI 服务（对外 HTTP 接口）
│   ├── main.py               # 应用入口 + 鉴权中间件
│   ├── deps.py               # 依赖注入
│   ├── ingest.py             # 入库调度（跨进程写锁 + 单任务）
│   ├── jobs.py               # 入库任务注册表（SQLite，跨进程可查）
│   └── routers/
│       ├── kb.py             # 知识库接口
│       └── ai_chat.py        # 问答接口
├── streamlit_file_uploader.py# Streamlit：知识库更新页
├── streamlit_qa.py           # Streamlit：问答页
├── requirements.txt
└── .env.example
```

运行时生成、**不纳入版本控制**的目录：`Lightrag_db/`（向量索引与缓存）、`chat_history/`（会话记录，含 `.locks/` 会话锁）、`crawler_state/`（新闻状态库、入库任务库 `rag_jobs.db` 与写锁文件）、`md5.text`（入库指纹）。

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

编辑 `.env` 填入 `DASHSCOPE_API_KEY`（或改用其他兼容 OpenAI 协议的服务，同时调整 `DASHSCOPE_BASE_URL`）。
`.env` 已被 `.gitignore` 忽略，**切勿提交**。也可改用系统环境变量，二者等价。

### 3. 启动 Qdrant

在仓库根目录用 compose 起（推荐，数据在 named volume 里，删容器不丢）：

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

### 5. 启动 Streamlit 页面（可选）

```bash
streamlit run streamlit_file_uploader.py   # 知识库更新
streamlit run streamlit_qa.py              # 问答
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 存活探针 |
| `POST` | `/v1/kb/upload` | 上传文档入库，`multipart/form-data`，字段 `file`；返回 `job_id` |
| `GET` | `/v1/kb/ingest/{job_id}` | 轮询入库进度（已完成任务保留 1 小时，最多 20 个） |
| `POST` | `/ai/chat` | 流式问答，返回 `text/event-stream` |

`POST /ai/chat` 请求体：

```json
{
  "user_id": 1,
  "session_id": "user_001",
  "message": "你好",
  "top_k": 20
}
```

`session_id` 仅允许字母、数字、下划线、连字符（长度 1–128）。

### 鉴权

设置 `RAG_API_KEY` 后，除 `/health`、`/docs`、`/redoc`、`/openapi.json` 外的接口都需携带请求头：

```
X-API-Key: <RAG_API_KEY>
```

留空则不启用鉴权，**仅限内网/开发环境**。Java 后台与服务端共享同一密钥。

## 配置说明

配置集中在 `config_data.py`，均支持环境变量覆盖，常用项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | 大模型/向量模型密钥（必填） |
| `QDRANT_URL` / `QDRANT_API_KEY` | `http://localhost:6333` / 空 | 向量库连接 |
| `RAG_API_KEY` | 空 | 服务鉴权密钥 |
| `LIGHTRAG_CHUNK_TOKEN_SIZE` | `2400` | 每块最大 token，越大入库越快（LLM 调用次数更少） |
| `LIGHTRAG_TOP_K` | `20` | 检索候选数；知识库内容多时建议 15–20，过大易撑爆上下文 |
| `LIGHTRAG_MAX_HISTORY_TURNS` | `10` | 会话历史保留轮数 |
| `LIGHTRAG_MODE` | `hybrid` | 检索模式：`naive`/`local`/`global`/`hybrid`/`mix` |
| `INGEST_JOB_DB` | `./crawler_state/rag_jobs.db` | 入库任务状态库（跨进程可查） |
| `INGEST_LOCK_PATH` | `./crawler_state/ingest.lock` | 入库/删除共用的跨进程写锁 |
| `KB_DELETE_LOCK_TIMEOUT_S` | `30` | 删除文档时等待写锁的最长秒数 |

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

1. **A（已完成）**：任务状态、写锁、会话历史外置。解决的是「重启丢 job」
   和「并发写互踩」，不解决 KV 快照。
2. **B**：继续单写进程，多读进程。写进程独占 LightRAG；读进程只读 Qdrant +
   共享 KV（把 `JsonKVStorage` 换成 Redis/Postgres 实现）。需要改
   LightRAG 的 storage 装配，读进程还要能拿到最新的 doc_status。
3. **C**：把入库改成异步队列（API 只入队，单独 worker 消费），读写彻底分离。
   吞吐最高，但引入队列组件和最终一致性，适合确实有并发入库压力时再上。

在 B/C 落地前，请保持 `--workers 1`；单 worker 下的并发保护
（agent `_SEM`、入库互斥）都是进程内的，多 worker 时上限会变成
「worker 数 × 各自上限」。

## 注意

- 入库耗时主要来自「切块 + 逐块调 LLM 抽取实体/关系」，属正常现象；进度可通过 `/v1/kb/ingest/{job_id}` 查看。
- 提问返回空结果时，多为提示词超出模型上下文上限，可调小 `LIGHTRAG_TOP_K`、`LIGHTRAG_MAX_HISTORY_TURNS`。
