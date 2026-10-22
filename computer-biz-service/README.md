# computer-biz-service

AI 知识智能体的 **Java BFF**(Backend For Frontend) 与 React 前端。

项目主链路：上传文档 → Python 侧异步入库（切块、向量化、抽实体关系）→ 基于知识库做流式问答。
本模块是浏览器与 Python 上游之间的一层：对接 RAG 服务、把业务记录落进 MySQL、把上游的错误
翻译成前端认识的形状，并提供 Agent 问答、写操作确认与抓取动态的 Web 入口。

## 架构

```
浏览器 (React 18 + Vite, :5173)
      │  /api/rag/*
      ▼
Java BFF (Spring Boot 4, :8080)              ← 本模块
      │  /ai/chat  /ai/agent/chat  /v1/kb/*  /v1/news/*
      ▼
Python RAG 服务 (FastAPI + LightRAG, :8102)  ← 仓库内 ../LightRAG
      │
      ├── MySQL 8.4 (:3306)   业务数据（用户/任务/会话/消息/Agent 轨迹）
      └── Qdrant    (:6333)   向量库（Java 不直接碰）
```

**Java 这一层不是多余的中转**，它承担四件事：

1. **协议适配** —— 旧问答是 SSE 文本流，Agent 是 NDJSON；Java 把每一行翻译成浏览器可消费的 SSE 帧；
2. **数据落库** —— 上游不留业务痕迹（答案只流一次、job 只保留一段时间），业务记录只能在这一层攒；
3. **统一错误契约** —— 把上游的 422、404、502 都翻译成 `{"detail":"..."}`；
4. **对前端隐藏上游** —— 前端只认 `/api/rag/*`，上游换端口或换实现不影响前端。

## 技术栈

| 层 | 技术 | 备注 |
|---|---|---|
| BFF | Spring Boot 4.1.1、Java 21 | Boot 4 的坑见 [§ 踩过的坑](#踩过的坑) |
| Web | Spring MVC(servlet, Tomcat) + **WebFlux 的 WebClient** | MVC 当服务端，WebClient 只当流式客户端 |
| ORM | MyBatis-Plus 3.5.17（`mybatis-plus-spring-boot4-starter`） | 版本必须对上 Boot 4 |
| 数据库 | MySQL 8.4（严格模式） | utf8mb4 / InnoDB |
| 前端 | React 18 + Vite 7 + Ant Design 5 | `frontend/`，:5173 |
| 上游 | FastAPI + LightRAG + Qdrant + DashScope Qwen | 向量库、LLM 都在 Python 侧 |

> `pom.xml` 里同时有 `starter-webmvc` 和 `starter-webflux`，但这是个 **servlet 应用**。
> WebFlux 在这里只用来提供 `WebClient`，它的服务端自动配置被
> `@ConditionalOnWebApplication(REACTIVE)` 挡住了，不会生效。

## 快速开始

### 前置：MySQL 8.4

一次性建库、建账号（密码自己定，别用 root 的）：

```sql
CREATE DATABASE `computer_biz`      DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE `computer_biz_test` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;  -- 只有跑 mvn test 才用得上
CREATE USER 'computer_biz'@'localhost' IDENTIFIED BY '你的密码';
CREATE USER 'computer_biz'@'127.0.0.1' IDENTIFIED BY '你的密码';
GRANT ALL PRIVILEGES ON `computer_biz`.*      TO 'computer_biz'@'localhost';
GRANT ALL PRIVILEGES ON `computer_biz`.*      TO 'computer_biz'@'127.0.0.1';
GRANT ALL PRIVILEGES ON `computer_biz_test`.* TO 'computer_biz'@'localhost';
GRANT ALL PRIVILEGES ON `computer_biz_test`.* TO 'computer_biz'@'127.0.0.1';
FLUSH PRIVILEGES;
```

然后建表：把 `db/schema.sql` 全文在 `computer_biz` 跑一遍，在 `computer_biz_test` 再跑一遍
（`mysql -u root -p computer_biz < db/schema.sql`）。

不想在本机装 MySQL 时，用仓库根目录的 compose：

```bash
docker compose --profile mysql up -d   # 宿主机 :3307，首次启动自动建表
export DB_PORT=3307
export DB_PASSWORD=123456             # 与根目录 .env 的 DB_PASSWORD 一致
```

`computer_biz_test` 仍需单独创建，见 [docker/README.md](../docker/README.md)。

### 启动

四个进程按顺序起：**MySQL → Qdrant → Python(:8102) → Java(:8080) → Vite(:5173)**。
每一步都**新开一个终端**，命令里的 `cd` 以仓库根目录为起点（本模块位于
`computer-biz-service/`）。

```bash
# ① Qdrant（仓库根目录）
docker compose up -d

# ② Python 上游（新终端）
cd LightRAG
uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1

# ③ 本模块（新终端）：密码不进仓库，每次先设一次
cd computer-biz-service
export DB_PASSWORD=你的密码          # PowerShell: $env:DB_PASSWORD = "你的密码"
./mvnw spring-boot:run              # Windows: .\mvnw.cmd spring-boot:run

# ④ 前端（新终端，仍从仓库根目录）
cd computer-biz-service/frontend
npm install                          # 只有第一次要
npm run dev
```

首次启动 ② 前，需按 [../LightRAG/README.md](../LightRAG/README.md#快速开始) 安装 Python 依赖并配置
`LightRAG/.env`（`DASHSCOPE_API_KEY`、`QDRANT_URL`）。

启动 ③ 后，日志里按顺序应该出现：`HikariPool-1 - Start completed.` → 落库自检 ① → 落库自检 ②。
卡在哪一步就说明问题在哪一层，详见 `docs/落库设计说明.md`。

### 常用命令

```bash
./mvnw -B compile      # 只编译，最快的一道检查，不用数据库
./mvnw -B test         # 需要 computer_biz_test 库
./mvnw spring-boot:run "-Dspring-boot.run.arguments=--debug"   # 打自动配置报告，排查「bean 没装配上」
```

### 配置

数据库连接全部走占位符，默认值指向本机 `computer_biz` 库：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DB_HOST` | `localhost` | |
| `DB_PORT` | `3306` | compose MySQL 用 `3307` |
| `DB_NAME` | `computer_biz` | |
| `DB_USER` | `computer_biz` | |
| `DB_PASSWORD` | 空 | 必须自己设 |

上游地址在 `application.yaml` 的 `rag.base-url`，默认 `http://localhost:8102`。单文件上传上限 100MB。

`AGENT_BIZ_QUERY_KEY` 是 Python Agent 的 `biz_query` 工具回调 `/internal/rag/query` 时的密钥；
留空则该内部接口返回 503。Python 侧同名变量必须一致。

## 接口

### 前端 → BFF（`/api/rag` 前缀）

| 方法 | 路径 | 入参 | 返回 |
|---|---|---|---|
| POST | `/documents` | `multipart/form-data`（文件 + 额外参数） | `FileUploadResp`（含 `job_id`） |
| GET | `/tasks/{jobId}` | 路径变量 | `TaskQueryResp` |
| POST | `/chat` | `AiChatReq`（JSON） | `Flux<String>`，SSE 流 |
| POST | `/agent/chat` | `AgentChatReq`（JSON） | `Flux<String>`，SSE 帧内为事件 JSON |
| POST | `/agent/stage` | `multipart/form-data`（文件） | `RagStageResp`（含 `staged_id`） |
| POST | `/agent/confirm` | `AgentConfirmReq`（JSON） | `{"ok":true,"approved":...}` |
| GET | `/news/overview` | `limit` | 抓取统计、源状态、最近运行 |
| GET | `/news/items` | `status`/`source_id`/`q`/`page`/`page_size` | 文章分页 |
| GET | `/news/items/{id}` | 路径变量 | 文章详情 |

`controller` 里没有业务逻辑——不查库、不调上游、不判状态，薄到只剩转发；业务规则集中在 `service`。

### BFF → Python 上游

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 存活探针 |
| `POST` | `/v1/kb/upload` | 上传并入库，`multipart/form-data`，字段 `file` |
| `POST` | `/v1/kb/stage` | 只暂存，供 Agent 入库使用 |
| `GET` | `/v1/kb/ingest/{job_id}` | 轮询入库进度 |
| `POST` | `/ai/chat` | 固定链路流式问答，`text/event-stream` |
| `POST` | `/ai/agent/chat` | Agent 事件流，`application/x-ndjson` |
| `POST` | `/ai/agent/confirm` | 批准 / 拒绝写操作 |
| `GET` | `/v1/news/overview` | 抓取概览（只读） |
| `GET` | `/v1/news/items` | 抓取文章列表（只读） |
| `GET` | `/v1/news/items/{id}` | 抓取文章详情（只读） |

Agent 的身份只走请求头 `X-User-Id`（由 Java 注入，本轮恒为演示用户 1）；请求体里没有 `user_id`。

## 包结构

根包 `org.example.computerbizservice`，依赖方向**单向**：
`controller → service → client/mapper`。

```
├── controller/
│   ├── RagController               旧问答与文档入库入口
│   ├── AgentController             Agent 问答、暂存、确认
│   ├── NewsController              抓取动态只读接口
│   └── InternalRagQueryController  供 Agent biz_query 调用的受限查询
├── service/                        业务编排（项目的中枢）
│   ├── RagService                  旧链路编排：调上游 + 织入落库
│   ├── AgentChatService            Agent 链路：NDJSON 原样转发 + 旁路落库
│   ├── AgentStepService            chat_step 的单线程写入器
│   ├── IngestRecordService         入库任务落库 → ingest_task（就地更新）
│   ├── ChatRecordService           问答记录落库 → chat_session + chat_message（追加）
│   └── NewsService                 抓取动态只读代理
├── client/                         RagClient（唯一碰上游 HTTP 的地方）
│                                   Utf8Stream（字节流解码）
├── config/                         RagClientConfig / RagClientProperties
├── dto/                            request/rag、response/rag、agent/AgentEventView
├── entity/                         User / IngestTask / ChatSession / ChatMessage / ChatStep
├── mapper/                         5 个**故意为空**的接口（extends BaseMapper 已够用）
├── common/                         AppConstants、enums、PersistenceStartupCheck
├── util/                           TextUtils
├── exception/                      RagApiException、GlobalExceptionHandler
└── vo/                             ⬜ 空目录，留给「要返回给前端的形状」
```

`RagClient` 用两个客户端：`RestClient`（同步）跑上传、轮询、新闻与确认；`WebClient`（流式）跑两条问答。
问答那条路里最重的是自研的 `Utf8Stream` 字节流解码器，绕开 Spring 的 SSE 解析器、自己处理
UTF-8 汉字跨块截断——详见 `docs/流式问答链路详解.md`。

## Agent 链路

```
浏览器 ──/api/rag/agent/chat──► Java :8080 ──/ai/agent/chat──► FastAPI :8102
         SSE（data: 一行事件）                NDJSON（一行一个 JSON）
```

- **原样转发**：Java 不重新序列化、不过滤事件类型，一行 NDJSON 对应一个 SSE `data:` 帧；
  前端拿到的就是 Python 的原始事件契约。
- **终态判定**：Python 的 `BUSY` / `MODEL_ERROR` / `MAX_STEPS` 都是先发 `error` 再正常关流，
  因此只有见过 `done` 才算成功，不能只看 Reactor 的 `ON_COMPLETE`。
- **旁路落库**：`answer` 增量攒进 `chat_message`，`thought` / `tool_call` / `tool_result`
  攒进 `chat_step`；轨迹写入排在单线程队列上，保证先 INSERT 后 UPDATE，且不阻塞回答。
- **写操作确认**：`confirm_required` 事件不占用问答流；前端用普通按钮调
  `POST /api/rag/agent/confirm`，Agent 侧继续等待并执行或跳过。
- **文件暂存**：`/agent/stage` 只保存文件并返回 `staged_id`，正文不进入模型上下文；
  是否入库由用户在对话里明确触发，并再次经过确认。
- **业务查询**：`biz_query` 回调 `/internal/rag/query`，只接受白名单 `query` 与参数，
  身份由 Java 注入，未知参数返回 400；密钥未配置时接口整体关闭。

## 数据库

五张表（`db/schema.sql`）：

| 表 | 写入方 | 形态 |
|---|---|---|
| `user` | `PersistenceStartupCheck`（启动补写） | 1 行演示数据，**无登录** |
| `ingest_task` | `IngestRecordService` | 一行一任务，就地更新（上游返回的是快照不是事件流） |
| `chat_session` | `ChatRecordService.ensureSession` | 按需创建，无外键约束 |
| `chat_message` | `ChatRecordService` / `AgentChatService` | 一次问答**两行**（user + assistant） |
| `chat_step` | `AgentStepService` | Agent 的 thought / tool_call / tool_result 流水，旁路写入 |

几个反直觉的点：

- **同一轮问答的两行 `created_at` 完全相同** —— 一轮之内的先后只能靠 `id` 排，不能靠时间；
- **清空 `error` 只能写空串，不能写 null** —— MP 默认不把 null 字段写进 UPDATE，
  写 null 等于「这列不动」；而 `lambdaUpdate().set(...)` 是显式写列（null 也会写进去），
  **两套语义正好相反**；
- 状态列存**大写枚举名字符串**（`'DONE'`），不是数字；`ChatRole` 是唯一用小写的枚举
  （`'user'`/`'assistant'`），因为 role 的取值在接口和前端本来就是小写；
- `chat_step.run_id + call_id` 把 `tool_result` 对回 `tool_call`；`assistant_id` 把轨迹绑到
  具体的回答上，避免同一会话连续追问时挂错。

## 设计要点

**五条铁律**：

| # | 规则 | 违反了会怎样 |
|---|---|---|
| 1 | 依赖单向：`controller → service → client/mapper` | 循环依赖启动报错，或分层失效改一处崩一片 |
| 2 | `entity` 不出 service 层 | `password_hash` 被序列化给前端 = 事故 |
| 3 | `client` 只做协议转换，业务规则在 service | 换调用方就得复制一遍重试/轮询/落库逻辑 |
| 4 | 错误翻译只有一份实现（`RagApiException.fromUpstream`） | 两条路各写一份，早晚一条腐烂，而它偏偏是排查时唯一的线索 |
| 5 | **落库是旁路**：所有落库走 `safeWrite` / `settleAsync` / 单线程写入器 | DB 抖动变成用户的 500 → 诱导重传 → 同一份文档入库两次 |

**三个「唯一」**：`RagService` / `AgentChatService` 是业务编排场所；`RagController` 等是唯一 HTTP 入口、
`GlobalExceptionHandler` 是唯一异常出口；`RagClient` 是唯一碰上游 HTTP 的地方。

**两条链路的同步/异步分界**（不能挪）：

- 链路 A（上传 + 轮询）全程同步，`ingest_task` 一行随轮询**就地更新**；
- 链路 B（问答）在请求线程上**同步写**开头的痕迹（会话行 + user 行 + 空 assistant 行），
  之后的一切都是流的一部分，终态用 `doFinally` 甩到 `boundedElastic` 异步落库。
  把开头那次写库放进 `Flux.defer` 里，落库就成了流的一部分，DB 一抖会把用户的回答带崩。

## 踩过的坑

Spring Boot 4 / MyBatis-Plus 3.5.17 上抄教程最容易翻车的四点，`pom.xml` 和代码注释里都写明了：

1. **`RestClient` / `WebClient.Builder` 要单独加 starter** —— Boot 4 把各 HTTP 客户端的
   自动配置拆成了独立模块（本项目要用 `spring-boot-starter-restclient` 和
   `spring-boot-starter-webclient`），光有 `starter-webflux` 拿不到 `WebClient.Builder`；
2. **MyBatis-Plus 必须是 `mybatis-plus-spring-boot4-starter`** —— 同版本的
   `-spring-boot3-starter` 在 Boot 4 上会以一堆 `NoClassDefFoundError` 收场；
3. **`ServiceImpl` 的包名变了** —— `com.baomidou.mybatisplus.spring.service.impl.ServiceImpl`，
   老路径 `extension.service.impl` 在 3.5.17 里是空的；
4. **Jackson 升到 3（`tools.jackson` 包）**，但 DTO 上仍用 Jackson 2 的注解
   （`com.fasterxml.jackson.annotation.*`）。

另外两个实体层的坑（症状都是「不报错但结果不对」）：

- 实体**不要加 `@Builder` / `@AllArgsConstructor`** —— MyBatis 靠「无参构造 + setter」组装对象，
  加了全参构造就没有默认构造，查出来的对象字段全是 null 且不报错；
- 时间列**不加 `@TableField(fill=...)`** —— 值交给数据库的 `DEFAULT CURRENT_TIMESTAMP(3)`
  （可行的原因是 MP 默认字段策略是 NOT_NULL，值为 null 的字段压根不进 INSERT）。

## 已知边界

- **无登录**：业务用户固定为演示用户 `id=1`。前端传来的 `userId` 一律不信任，身份由 Java 注入。
- **上游超时未设**：两个 HTTP 客户端都没有显式读写超时；接公网或弱网前需要补。
- **客户端断开形态未单独实测**：`CANCEL` 与 `ClientAbortException` 两种都处理了。
- **无用户侧历史读取接口**：只写不读；内部 `/internal/rag/query` 仅供 Agent 的 `biz_query`。
- **单实例部署**：Java 侧没有分布式锁；多实例同时轮询同一 job 会重复落库。
- 空壳/未使用：`TaskQueryReq`（未使用）、`AiChatResp`（空且未使用）、`vo/`（空目录）。

Java 侧以「真实启动 + 看日志 + Navicat」为主，`mvn test` 只覆盖少数组件（UTF-8 解码、启动上下文）。
上传/轮询的全链路可以用 `frontend/tools/verify-serial.cjs` 复验。

## 相关文档

`docs/` 下的文档比这份 README 深得多，遇到具体问题优先翻它们：

| 文档 | 讲什么 |
|---|---|
| `项目整体说明.md` | 包结构与依赖关系，每个包的职责 + 为什么这么分 |
| `流式问答链路详解.md` | 字节流解码器 + 前端增量分帧，逐行代码 + 面试追问 |
| `流式问答状态机详解.md` | 五种终态、取消与中断的判定 |
| `rag对接实现说明.md` | 上游对接与联调时的坑（SSE 相关事故的原始记录） |
| `落库设计说明.md` | 五张表的设计、巡检 SQL |
| `落库改动清单.md` | 逐条编号的改动说明（每条带理由 + 验证方法） |
| `前端工程说明.md` / `前端要点详解.md` | 前端 SSE 坑、store 设计、状态机 |
| `MyBatis 理解.md` | MyBatis / MyBatis-Plus 的概念笔记 |
| `start.md`（模块根目录） | 启动手册 |
| `../LightRAG/README.md` | Python 上游的接口、Agent、配置 |
| `../README.md` | 项目总览 |
