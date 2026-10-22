# AI 知识智能体

面向企业文档与 AI 前沿进展的知识智能体。文档经解析、切块、实体关系抽取后写入图谱与向量库；问答侧提供两条链路——固定检索问答与 Agent 自主工具循环；另有独立进程持续采集官方 AI 发布，筛选后进入同一知识库。

项目由 **四个进程** 组成，分属三个技术栈，依赖方向单向：

- **React 前端** 只认 `/api/rag/*`，负责交互、状态机与增量渲染；
- **Java BFF** 是中间层，负责协议适配、业务落库、统一错误契约；
- **Python RAG 服务** 持有向量库与 LLM，负责入库、检索、问答与 Agent 运行时；
- **news_ingest** 是独立采集进程，不导入 RAG 服务，只通过 HTTP 把文章交给知识库。

## 全景架构

```
浏览器 (React 18 + Vite, :5173)
      │  /api/rag/*
      ▼
Java BFF (Spring Boot 4, :8080)              ← computer-biz-service
      │  /ai/chat  /ai/agent/chat  /v1/kb/*  /v1/news/*
      ▼
Python RAG 服务 (FastAPI + LightRAG, :8102)  ← LightRAG
      │
      ├── Qdrant (:6333)          向量库
      ├── Lightrag_db/            图/向量索引与 doc_status（文件型 KV）
      └── crawler_state/          入库任务库、新闻状态库、写锁

news_ingest（独立进程）── POST /v1/kb/upload ──► Python RAG 服务

MySQL 8.4 (:3306，compose 默认 :3307)  ← 仅 Java BFF 访问
      用户 / 入库任务 / 会话 / 消息 / Agent 决策轨迹
```

**为什么中间要多一层 Java BFF**（不是多余的中转），它承担四件事：

1. **协议适配** —— 上游问答是 SSE 文本流、Agent 是 NDJSON；浏览器无法用 `EventSource` 发 POST，需要一层翻译成前端可消费的 SSE 帧；
2. **数据落库** —— 上游不留业务痕迹（答案只流一次、job 只保留一段时间），会话、消息、Agent 轨迹只能在这一层攒；
3. **统一错误契约** —— 把上游的 422、404、502 统一翻译成 `{"detail":"..."}`；
4. **对前端隐藏上游** —— 上游换端口、换实现不影响前端。

## 主要功能

### 文档入库

- 上传文档 → 解析为文本 → LightRAG 抽取实体/关系 → 写入 Qdrant 与本地图索引；
- 内容 MD5 指纹去重，重复上传直接跳过；
- SQLite 任务注册表 + 跨进程写锁：API 重启不丢 job，入库与删除单写者互斥；
- 以 doc_status **终态** 判定成功：失败不写指纹，修复后可重试。

### 两条问答链路

| 链路 | 入口 | 特点 |
|---|---|---|
| 固定检索问答 | `POST /api/rag/chat` | 检索 → 拼接上下文 → 流式生成，支持会话历史 |
| Agentic RAG | `POST /api/rag/agent/chat` | 最多 6 步 ReAct 循环，模型自主选择工具与检索模式 |

Agent 工具：`kb_search`、`kb_status`、`calculator`、`now`，按需启用 `web_search`、`biz_query`；`kb_ingest`、`kb_delete` 默认不对模型可见。

### 写操作的人类在环（HITL）

- 写工具默认隐藏；开启后执行前仍需预检与人工确认，确认请求有 TTL；
- 上传给 Agent 只暂存文件标识，正文永不进入模型上下文；
- `biz_query` 只暴露白名单查询与参数，SQL 与身份由 Java 侧注入。

### AI 前沿知识采集

- 独立进程跟踪 8 个启用的官方源，支持 RSS、Atom 与 sitemap；
- 条件请求、URL 规范化去重、规则预筛 + 一次模型分类；
- 正文按不可信数据处理，失败一律拒绝入库；
- 超过 7 天未处理的条目只标记 `filtered_out`，不删行。

### Web 页面

「文档入库」「知识库问答」「Agent 问答」「抓取动态」四个页签，对应四条独立链路。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | React 18、Vite 7、Ant Design 5 |
| BFF | Java 21、Spring Boot 4.1、Spring MVC、WebClient、MyBatis-Plus 3.5.17 |
| 数据库 | MySQL 8.4（utf8mb4 / InnoDB） |
| 上游 | Python 3.13、FastAPI、Pydantic、LightRAG 1.5.5、Qdrant |
| 模型 | DashScope 兼容接口（默认 `qwen-max` + `text-embedding-v4`） |
| 流式 | SSE（前端 ↔ Java）、NDJSON（Java ↔ Python） |
| 基础设施 | Docker Compose（Qdrant + 可选 MySQL）、SQLite WAL |

## 仓库结构

```text
.
├── LightRAG/                     Python 上游
│   ├── rag_api/                  FastAPI 应用与路由
│   ├── agent/                    Agentic RAG 运行时
│   ├── news_ingest/              独立采集进程
│   ├── tests/                    107 个单元测试
│   └── README.md                 Python 侧详细说明
├── computer-biz-service/         Java BFF
│   ├── src/main/java/            控制器 / 服务 / 上游客户端
│   ├── frontend/                 React 前端
│   ├── db/schema.sql             5 张业务表
│   └── README.md                 Java 侧详细说明
├── docker/                       MySQL 初始化脚本
├── docker-compose.yml            Qdrant（默认）+ MySQL（profile）
├── .env.example                  compose 变量（MySQL 密码、端口）
└── README.md
```

运行时目录（不纳入版本控制）：`LightRAG/Lightrag_db/`、`LightRAG/chat_history/`、`LightRAG/crawler_state/`、`LightRAG/uploads/`、`LightRAG/agent_runs/`、`LightRAG/md5.text`。

## 快速开始

### 依赖

- Python 3.10+（开发环境 3.13）
- Java 21
- Node.js 18+
- Docker（跑 Qdrant，可选 MySQL）
- 一个 DashScope API Key（或兼容 OpenAI 协议的模型服务）

### ① 启动基础设施

```bash
# 只起 Qdrant（宿主机 :6333）
docker compose up -d

# 同时起 MySQL（宿主机 :3307，避开本机 3306）
docker compose --profile mysql up -d
```

首次从旧的 `docker run qdrant` 容器迁移、数据卷与端口覆盖见 [docker/README.md](docker/README.md)。

> 下面 ②③④ 假设都从**仓库根目录**开始；每个进程建议单独开一个终端，
> 不要在同一个终端里连续 `cd`。

### ② Python RAG 服务

```bash
cd LightRAG
pip install -r requirements.txt
cp .env.example .env          # 填入 DASHSCOPE_API_KEY

uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1
```

> **必须单 worker。** LightRAG 的 KV/doc_status 是文件型存储、进程内各持一份内存快照；多 worker 会互相覆盖。任务状态与写锁已经跨进程，但 KV 还没有。

### ③ Java BFF

```bash
cd computer-biz-service        # 新终端，从仓库根目录进入

# 密码不进仓库，每个新终端设一次
export DB_PASSWORD=你的密码        # PowerShell: $env:DB_PASSWORD = "你的密码"

# 使用 compose 的 MySQL 时改端口
export DB_PORT=3307                # PowerShell: $env:DB_PORT = "3307"

./mvnw spring-boot:run             # Windows: .\mvnw.cmd spring-boot:run
```

首次启动前需要建库、建账号并执行 `db/schema.sql`，完整 SQL 见
[computer-biz-service/README.md](computer-biz-service/README.md#快速开始)。

使用 compose MySQL 时，`db/schema.sql` 会在数据卷首次初始化时自动执行，无需手工建表。

启动日志中依次出现 `HikariPool-1 - Start completed.`、落库自检 ①、落库自检 ②，说明数据库与演示数据正常。

### ④ 前端

```bash
cd computer-biz-service/frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。Vite 已把 `/api` 代理到 8080，开发期不需要 CORS。

### ⑤ 可选：启动 AI 前沿采集

```bash
cd LightRAG                    # 新终端从仓库根目录进入

# 只检查源可达性，不写库、不调模型
python -m news_ingest --check-sources

# 跑一轮（API 必须在线；文章经 /v1/kb/upload 入库）
python -m news_ingest --once

# 常驻循环
python -m news_ingest --loop --interval-minutes 360
```

更多参数、状态机与重试策略见 [LightRAG/news_ingest/README.md](LightRAG/news_ingest/README.md)。

### 交互文档

- Python 上游：<http://localhost:8102/docs>
- 前端页面：<http://localhost:5173>

## 配置

### Python（`LightRAG/.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | 大模型 / 向量模型密钥（必填） |
| `DASHSCOPE_BASE_URL` | DashScope 兼容接口 | 可替换为其他 OpenAI 兼容服务 |
| `LLM_MODEL` | `qwen-max` | 生成模型 |
| `QDRANT_URL` / `QDRANT_API_KEY` | `http://localhost:6333` / 空 | 向量库连接 |
| `RAG_API_KEY` | 空 | 上游鉴权；留空仅限内网 |
| `AGENT_ENABLED` | `1` | 关闭后 `/ai/agent/chat` 返回结构化 `AGENT_DISABLED` |
| `AGENT_ALLOW_WRITE_TOOLS` | `0` | 写工具总闸门 |
| `AGENT_REQUIRE_CONFIRM` | `1` | 写操作人工确认 |
| `AGENT_BIZ_QUERY_URL` / `AGENT_BIZ_QUERY_KEY` | Java 内部接口 / 空 | 未配置 key 时不注册 `biz_query` |

完整变量见 `LightRAG/.env.example` 与 [LightRAG/README.md](LightRAG/README.md)。

### Java（环境变量）

| 变量 | 默认值 |
|---|---|
| `DB_HOST` | `localhost` |
| `DB_PORT` | `3306`（compose MySQL 用 `3307`） |
| `DB_NAME` | `computer_biz` |
| `DB_USER` | `computer_biz` |
| `DB_PASSWORD` | 空（必须自己设） |
| `AGENT_BIZ_QUERY_KEY` | 空（为空则内部查询接口关闭） |

上游地址在 `application.yaml` 的 `rag.base-url`，默认 `http://localhost:8102`。单文件上传上限 100MB。

## 接口

### 前端 → Java BFF（`/api/rag` 前缀）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/documents` | 上传文档，返回 `job_id` |
| `GET` | `/tasks/{jobId}` | 轮询入库进度 |
| `POST` | `/chat` | 固定检索问答，SSE 流 |
| `POST` | `/agent/chat` | Agent 问答，SSE 帧内为事件 JSON |
| `POST` | `/agent/stage` | 只暂存文件，返回 `staged_id` |
| `POST` | `/agent/confirm` | 批准 / 拒绝写操作 |
| `GET` | `/news/overview` | 抓取统计、源状态、最近运行 |
| `GET` | `/news/items` | 抓取文章分页列表 |
| `GET` | `/news/items/{id}` | 文章详情 |

### Java BFF → Python 上游

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 存活探针 |
| `POST` | `/v1/kb/upload` | 上传并入库，`multipart/form-data`，字段 `file` |
| `POST` | `/v1/kb/stage` | 只暂存，供 Agent 入库使用 |
| `GET` | `/v1/kb/ingest/{job_id}` | 轮询入库进度 |
| `POST` | `/ai/chat` | 固定链路流式问答，`text/event-stream` |
| `POST` | `/ai/agent/chat` | Agent 事件流，`application/x-ndjson` |
| `POST` | `/ai/agent/confirm` | 写操作确认 |
| `GET` | `/v1/news/overview` | 抓取概览（只读） |
| `GET` | `/v1/news/items` | 抓取文章列表（只读） |
| `GET` | `/v1/news/items/{id}` | 抓取文章详情（只读） |

`/ai/agent/chat` 的身份只从 `X-User-Id` 请求头读取，请求体里的 `user_id` 一律不认。

## 数据库

MySQL 共 5 张表（`computer-biz-service/db/schema.sql`）：

| 表 | 写入方 | 形态 |
|---|---|---|
| `user` | 启动自检 | 1 行演示数据，无登录 |
| `ingest_task` | `IngestRecordService` | 一行一任务，随轮询就地更新 |
| `chat_session` | `ChatRecordService` | 按需创建 |
| `chat_message` | `ChatRecordService` | 一次问答两行（user + assistant） |
| `chat_step` | `AgentStepService` | Agent 的 thought / tool_call / tool_result 轨迹，旁路写入 |

Agent 轨迹写入排在单线程队列上，保证 `tool_call` 先 INSERT、`tool_result` 后 UPDATE；任何落库失败只打日志，不影响正在生成的回答。

## 测试

```bash
# Python：107 个单元测试，不依赖在线模型与向量库
cd LightRAG
python -m unittest discover -s tests -t .

# Java：编译 / 单测
cd computer-biz-service
./mvnw -B compile
./mvnw -B test          # 需要 computer_biz_test 库

# 前端：构建 + 离线回归脚本
cd computer-biz-service/frontend
npm run build

node tools/verify-chat.mjs          # SSE 分帧与状态机
node tools/verify-agent.mjs         # Agent 事件归纳与确认流程
node tools/verify-http-errors.mjs   # 错误文案
```

Java 侧以「真实启动 + 看日志 + Navicat」为主，`mvn test` 只覆盖少数组件；前端脚本大多数不需要后端。

## 设计要点

- **HTTP 200 ≠ 成功**：Python 的 `BUSY` / `MODEL_ERROR` / `MAX_STEPS` 都是先发 `error` 事件再正常关流。Java 与前端都以「收到 `done`」作为唯一成功信号。
- **入库以终态为准**：LightRAG 的 `ainsert` 在实体抽取失败时仍返回任务号，因此不能凭返回值记成功；失败不写内容指纹，保证可重试。
- **单写者**：入库、删除、会话历史各有一把锁；任务状态放 SQLite，API 重启不丢 job。
- **落库是旁路**：任何业务落库失败都不能把用户的回答带崩，错误只进日志。
- **不可信数据**：抓取正文与知识库内容都按数据处理，提示词注入不执行。

## 已知边界

- 没有登录体系，业务用户固定为演示用户 `id=1`；不信任请求里自带的 `userId`。
- Python API 必须单 worker；LightRAG KV 尚未换成共享后端。
- 当前只接受 `.txt` 文档（`config_data.py` 的 `supported_file_extensions`）。
- Java 到上游的两个 HTTP 客户端尚未设置超时。
- 用户侧没有历史会话查询接口；内部查询接口 `/internal/rag/query` 只供 Agent 的 `biz_query` 使用。

## 相关文档

| 文档 | 内容 |
|---|---|
| [LightRAG/README.md](LightRAG/README.md) | Python 上游：接口、Agent、配置、扩容路线 |
| [LightRAG/news_ingest/README.md](LightRAG/news_ingest/README.md) | 采集进程：源、过滤、状态机、定时任务 |
| [computer-biz-service/README.md](computer-biz-service/README.md) | Java BFF：包结构、落库、踩坑 |
| [docker/README.md](docker/README.md) | Compose、数据卷、MySQL 初始化 |
| `LightRAG/docs/Agentic RAG 改造说明.md` | Agent 循环、事件协议、写操作确认 |
| `computer-biz-service/docs/流式问答链路详解.md` | 字节流解码与前端分帧 |
| `computer-biz-service/docs/落库设计说明.md` | 5 张表与巡检 SQL |
