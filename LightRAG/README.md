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
│   ├── ingest.py             # 入库任务调度（进程内单任务 + 互斥锁）
│   └── routers/
│       ├── kb.py             # 知识库接口
│       └── ai_chat.py        # 问答接口
├── streamlit_file_uploader.py# Streamlit：知识库更新页
├── streamlit_qa.py           # Streamlit：问答页
├── requirements.txt
└── .env.example
```

运行时生成、**不纳入版本控制**的目录：`Lightrag_db/`（向量索引与缓存）、`chat_history/`（会话记录）、`md5.text`（入库指纹）。

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

```bash
docker run -d --name qdrant -p 6333:6333 qdrant/qdrant
```

云服务则在 `.env` 中填写 `QDRANT_URL` 与 `QDRANT_API_KEY`。

### 4. 启动 API 服务

```bash
uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1
```

> **必须单 worker。** 服务内部依赖常驻事件循环与进程内单例，多 worker 会导致多个实例并发写同一工作目录。

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
| `GET` | `/v1/kb/ingest/{job_id}` | 轮询入库进度（仅保留最近一个任务） |
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

支持的文件类型见 `config_data.py` 的 `supported_file_extensions`（当前仅 `.txt`）。

## 注意

- 入库耗时主要来自「切块 + 逐块调 LLM 抽取实体/关系」，属正常现象；进度可通过 `/v1/kb/ingest/{job_id}` 查看。
- 提问返回空结果时，多为提示词超出模型上下文上限，可调小 `LIGHTRAG_TOP_K`、`LIGHTRAG_MAX_HISTORY_TURNS`。
