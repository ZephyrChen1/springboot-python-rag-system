\# 企业知识库 + 大模型问答平台



一个全栈 RAG 应用：用户上传文档 → 后台异步入库(切片、向量化、抽实体关系)→ 基于知识库做流式问答，答案逐字吐给前端。



项目由\*\*三个进程\*\*组成，分属三个技术栈：React 前端、Java BFF、Python RAG 服务。三者职责单一、单向依赖：



\- \*\*前端\*\*只认 `/api/rag/\*`，负责交互与增量渲染；

\- \*\*Java BFF\*\* 是中间层，做协议适配、业务落库、统一错误契约；

\- \*\*Python 上游\*\*真正持有向量库与 LLM，负责入库与检索问答。



\## 全景架构



```

浏览器 (React 18 + Vite, :5173)

&#x20;     │  /api/rag/\*

&#x20;     ▼

Java BFF (Spring Boot 4, :8080)              ← computer-biz-service

&#x20;     │  /ai/chat   /v1/kb/\*

&#x20;     ▼

Python RAG 服务 (FastAPI + LightRAG, :8102)   ← 本仓库 Python 侧

&#x20;     │

&#x20;     ├── MySQL 8.4  (:3306)   业务数据(用户 / 任务 / 会话 / 消息)

&#x20;     └── Qdrant     (:6333)   向量库(Java 不直接碰)

```



\*\*为什么中间要多一层 Java BFF\*\*(不是多余的中转)，它承担四件事：



1\. \*\*协议适配\*\* —— 上游流式响应是非标准 SSE(裸文本)，浏览器 `EventSource` 吃不下，需要一层把字节流翻译成前端能用的分帧格式；

2\. \*\*数据落库\*\* —— 上游不留痕(答案只流一次、job 只保留最近一个)，业务记录只能在这一层攒；

3\. \*\*统一错误契约\*\* —— 把上游的 422 校验错误、404、502 都翻译成 `{"detail":"..."}`；

4\. \*\*对前端隐藏上游\*\* —— 前端只认 `/api/rag/\*`，上游换端口/换实现不影响前端。



\## 技术栈



| 层 | 技术 | 备注 |

|---|---|---|

| 前端 | React 18 + Vite 7 + Ant Design 5 | :5173 |

| BFF | Spring Boot 4.1.1、Java 21 | Boot 4 的坑见 \[踩过的坑](#踩过的坑) |

| BFF Web | Spring MVC(servlet, Tomcat) + \*\*WebFlux 的 WebClient\*\* | MVC 当服务端，WebClient 只当流式客户端 |

| ORM | MyBatis-Plus 3.5.17(`mybatis-plus-spring-boot4-starter`) | 版本必须对上 Boot 4 |

| 数据库 | MySQL 8.4(严格模式) | utf8mb4 / InnoDB |

| 上游 | FastAPI + LightRAG + Qdrant + DashScope Qwen | 向量库、LLM 都在 Python 侧 |



> Java 侧 `pom.xml` 里同时有 `starter-webmvc` 和 `starter-webflux`，但这是个 \*\*servlet 应用\*\*。

> WebFlux 在这里只用来提供 `WebClient`，它的服务端自动配置被 `@ConditionalOnWebApplication(REACTIVE)` 挡住，不会生效。



\## 各模块职责



\### 前端（`frontend/`，:5173）



\- SSE 增量渲染：把 BFF 的分帧流解出来逐字上屏；

\- 状态机：`chatStore.MSG`(5 值)与 `ingestStore.STATUS`(服务端 5 值 + 3 个纯客户端状态 `queued`/`uploading`/`committing`)；

\- 上传/轮询交互、错误提示。



\### Java BFF（computer-biz-service，:8080）



一个薄 BFF：`controller` 里没有任何业务逻辑——不查库、不调上游、不判状态，薄到只剩转发；业务规则集中在 `service`。



根包 `org.example.computerbizservice`，依赖方向\*\*单向\*\*：`controller → service → client/mapper`。



```

├── controller/    RagController              唯一的 HTTP 入口

├── service/       业务编排(项目的中枢)

│   ├── RagService             主链路编排:调上游 + 织入落库;唯一同时持有 client 和两个 record service 的类

│   ├── IngestRecordService    入库任务的落库    → ingest\_task(就地更新)

│   └── ChatRecordService      问答记录的落库    → chat\_session + chat\_message(追加)

├── client/        RagClient                  唯一碰上游 HTTP 的地方,只做协议转换

├── config/        RagClientConfig / RagClientProperties   装配两个 HTTP 客户端 Bean

├── dto/           request/rag、response/rag   纯数据,无逻辑

├── entity/        User / IngestTask / ChatSession / ChatMessage

├── mapper/        4 个\*\*故意为空\*\*的接口(extends BaseMapper 已够用)

├── common/        AppConstants、enums、PersistenceStartupCheck(启动自检)

├── util/          TextUtils

├── exception/     RagApiException、GlobalExceptionHandler(唯一的异常出口)

└── vo/            ⬜ 空目录,留给「要返回给前端的形状」

```



`RagClient` 用两个客户端：`RestClient`(同步)跑上传和轮询，`WebClient`(流式)跑问答。问答那条路里最重的是自研的 `decodeRawText()` 字节流解码器，绕开 Spring 的 SSE 解析器、自己处理 UTF-8 汉字跨块截断。



\### Python RAG 服务（:8102）



基于 \[LightRAG](https://github.com/HKUDS/LightRAG) 的知识库检索与问答服务，向 Java 后台提供 HTTP 接口。提供两条链路：



\- \*\*入库\*\*：上传文档 → 解析为文本 → LightRAG 抽取实体/关系并写入向量库(Qdrant)

\- \*\*问答\*\*：结合会话历史的流式检索问答(SSE 流式返回)



```

.

├── config\_data.py            # 全局配置(模型、向量库、分块与检索参数)

├── lightrag\_service.py       # LightRAG 封装:常驻事件循环 + 全局单例

├── knowledge\_base.py         # 入库服务:MD5 去重 + 入库进度读取

├── document\_loader.py        # 文档解析为纯文本

├── file\_history\_store.py     # 会话历史文件存储

├── rag.py                    # RagService:组合检索与历史,产出流式回答

├── rag\_api/                  # FastAPI 服务(对外 HTTP 接口)

│   ├── main.py               # 应用入口 + 鉴权中间件

│   ├── deps.py               # 依赖注入

│   ├── ingest.py             # 入库任务调度(进程内单任务 + 互斥锁)

│   └── routers/

│       ├── kb.py             # 知识库接口

│       └── ai\_chat.py        # 问答接口

├── streamlit\_file\_uploader.py# Streamlit:知识库更新页

├── streamlit\_qa.py           # Streamlit:问答页

├── requirements.txt

└── .env.example

```



运行时生成、\*\*不纳入版本控制\*\*的目录：`Lightrag\_db/`(向量索引与缓存)、`chat\_history/`(会话记录)、`md5.text`(入库指纹)。



\## 快速开始



\### 依赖与准备



\- \*\*Python 3.10+\*\*(开发环境 3.13)、\*\*Java 21\*\*、\*\*Node.js\*\*(前端)、\*\*MySQL 8.4\*\*、\*\*Docker\*\*(跑 Qdrant)

\- 一个 OpenAI 兼容的大模型与向量模型服务(默认 DashScope)



\### ① 建库、建账号（MySQL）



一次性对 root 连接执行(密码自己定，别用 root 的)：



```sql

CREATE DATABASE `computer\_biz`      DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4\_0900\_ai\_ci;

CREATE DATABASE `computer\_biz\_test` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4\_0900\_ai\_ci;  -- 只有跑 mvn test 才用得上

CREATE USER 'computer\_biz'@'localhost' IDENTIFIED BY '你的密码';

CREATE USER 'computer\_biz'@'127.0.0.1' IDENTIFIED BY '你的密码';

GRANT ALL PRIVILEGES ON `computer\_biz`.\*      TO 'computer\_biz'@'localhost';

GRANT ALL PRIVILEGES ON `computer\_biz`.\*      TO 'computer\_biz'@'127.0.0.1';

GRANT ALL PRIVILEGES ON `computer\_biz\_test`.\* TO 'computer\_biz'@'localhost';

GRANT ALL PRIVILEGES ON `computer\_biz\_test`.\* TO 'computer\_biz'@'127.0.0.1';

FLUSH PRIVILEGES;

```



然后建表：把 Java 侧 `db/schema.sql` 全文在 `computer\_biz` 跑一遍，在 `computer\_biz\_test` 再跑一遍。(本轮没有迁移工具，以后改表要改这个文件 + 在库上手写一条 `ALTER`。)



\### ② Python 上游：装依赖、配密钥、起 Qdrant



```bash

pip install -r requirements.txt

cp .env.example .env          # 填入 DASHSCOPE\_API\_KEY

docker run -d --name qdrant -p 6333:6333 qdrant/qdrant

```



`.env` 已被 `.gitignore` 忽略，\*\*切勿提交\*\*；也可改用系统环境变量，二者等价。Qdrant 用云服务则在 `.env` 中填 `QDRANT\_URL` 与 `QDRANT\_API\_KEY`。



\### ③ 启动



四个进程按顺序起：\*\*MySQL → Qdrant → FastAPI(:8102) → Java(:8080) → Vite(:5173)\*\*。



```bash

\# ① Python 上游

uvicorn rag\_api.main:app --host 0.0.0.0 --port 8102 --workers 1



\# ② Java BFF:密码不进仓库,每个新终端先设一次

export DB\_PASSWORD=你的密码          # PowerShell: $env:DB\_PASSWORD = "你的密码"

./mvnw spring-boot:run              # Windows: .\\mvnw.cmd spring-boot:run



\# ③ 前端

cd frontend

npm install                          # 只有第一次要

npm run dev

```



> \*\*上游必须单 worker。\*\* 服务内部依赖常驻事件循环与进程内单例，多 worker 会导致多个实例并发写同一工作目录。



启动 Java 后，日志里按顺序应出现：`HikariPool-1 - Start completed.` → 落库自检 ① → 落库自检 ②。卡在哪一步就说明问题在哪一层。



\### 可选：Streamlit 管理/体验页



```bash

streamlit run streamlit\_file\_uploader.py   # 知识库更新

streamlit run streamlit\_qa.py              # 问答

```



\### 交互文档



\- Python 上游：<http://localhost:8102/docs>



\## 配置



\### Java BFF



数据库连接全部走占位符，默认值指向本机 `computer\_biz` 库：



| 环境变量 | 默认值 |

|---|---|

| `DB\_HOST` | `localhost` |

| `DB\_PORT` | `3306` |

| `DB\_NAME` | `computer\_biz` |

| `DB\_USER` | `computer\_biz` |

| `DB\_PASSWORD` | 空(必须自己设) |



上游地址在 `application.yaml` 的 `rag.base-url`，默认 `http://localhost:8102`。单文件上传上限已从默认 1MB 调到 \*\*100MB\*\*(知识库文档)。



\### Python 上游



配置集中在 `config\_data.py`，均支持环境变量覆盖，常用项：



| 变量 | 默认值 | 说明 |

|---|---|---|

| `DASHSCOPE\_API\_KEY` | — | 大模型/向量模型密钥(必填) |

| `QDRANT\_URL` / `QDRANT\_API\_KEY` | `http://localhost:6333` / 空 | 向量库连接 |

| `RAG\_API\_KEY` | 空 | 服务鉴权密钥 |

| `LIGHTRAG\_CHUNK\_TOKEN\_SIZE` | `2400` | 每块最大 token，越大入库越快(LLM 调用次数更少) |

| `LIGHTRAG\_TOP\_K` | `20` | 检索候选数；知识库内容多时建议 15–20，过大易撑爆上下文 |

| `LIGHTRAG\_MAX\_HISTORY\_TURNS` | `10` | 会话历史保留轮数 |

| `LIGHTRAG\_MODE` | `hybrid` | 检索模式：`naive`/`local`/`global`/`hybrid`/`mix` |



支持的文件类型见 `config\_data.py` 的 `supported\_file\_extensions`(当前仅 `.txt`)。



\### 鉴权



Python 侧设置 `RAG\_API\_KEY` 后，除 `/health`、`/docs`、`/redoc`、`/openapi.json` 外的接口都需携带请求头 `X-API-Key: <RAG\_API\_KEY>`。留空则不启用鉴权，\*\*仅限内网/开发环境\*\*。Java BFF 与服务端共享同一密钥。



\## 接口



\### 前端 → Java BFF(`/api/rag` 前缀)



三个接口，与上游 FastAPI 的 `/ai/chat`、`/v1/kb/\*` 是\*\*两套独立命名\*\*，Java 可自由重组 API 形状，不用跟着上游改。



| 方法 | 路径 | 入参 | 返回 |

|---|---|---|---|

| POST | `/api/rag/documents` | `multipart/form-data`(文件 + 额外参数) | `FileUploadResp`(含 `job\_id`) |

| GET | `/api/rag/tasks/{jobId}` | 路径变量 | `TaskQueryResp` |

| POST | `/api/rag/chat` | `AiChatReq`(JSON) | `Flux<String>`，SSE 流 |



\### Java BFF → Python 上游



| 方法 | 路径 | 说明 |

|---|---|---|

| `GET` | `/health` | 存活探针 |

| `POST` | `/v1/kb/upload` | 上传文档入库，`multipart/form-data`，字段 `file`；返回 `job\_id` |

| `GET` | `/v1/kb/ingest/{job\_id}` | 轮询入库进度(仅保留最近一个任务) |

| `POST` | `/ai/chat` | 流式问答，返回 `text/event-stream` |



`POST /ai/chat` 请求体：



```json

{

&#x20; "user\_id": 1,

&#x20; "session\_id": "user\_001",

&#x20; "message": "你好",

&#x20; "top\_k": 20

}

```



`session\_id` 仅允许字母、数字、下划线、连字符(长度 1–128)。



\## 数据库



四张表(`db/schema.sql`)：



| 表 | 写入方 | 形态 |

|---|---|---|

| `user` | `PersistenceStartupCheck`(启动补写) | 1 行演示数据，\*\*无登录\*\* |

| `ingest\_task` | `IngestRecordService` | 一行一任务，就地更新(上游返回的是快照不是事件流) |

| `chat\_session` | `ChatRecordService.ensureSession` | 按需创建，无外键约束 |

| `chat\_message` | `ChatRecordService` | 一次问答\*\*两行\*\*(user + assistant) |



三个反直觉的点：



\- \*\*同一轮问答的两行 `created\_at` 完全相同\*\* —— 一轮之内的先后只能靠 `id` 排，不能靠时间；

\- \*\*清空 `error` 只能写空串，不能写 null\*\* —— MP 默认不把 null 字段写进 UPDATE，写 null 等于「这列不动」；而 `lambdaUpdate().set(...)` 是显式写列(null 也会写进去)，\*\*两套语义正好相反\*\*；

\- 状态列存\*\*大写枚举名字符串\*\*(`'DONE'`)，不是数字；`ChatRole` 是唯一用小写的枚举(`'user'`/`'assistant'`)，因为 role 的取值在接口和前端本来就是小写。



⚠️ 枚举值与前端状态机\*\*一一对应，不要自己加第六个\*\*：`ChatMsgStatus` 5 值 ↔ 前端 `chatStore.MSG`；`IngestStatus` 5 值 ↔ 前端 `ingestStore.STATUS` 的后 5 个(前端多 3 个纯客户端状态 `queued`/`uploading`/`committing`，服务端永远看不到)。



\## 设计要点



\*\*五条铁律\*\*：



| # | 规则 | 违反了会怎样 |

|---|---|---|

| 1 | 依赖单向：`controller → service → client/mapper` | 循环依赖启动报错，或分层失效改一处崩一片 |

| 2 | `entity` 不出 service 层 | `password\_hash` 被序列化给前端 = 事故 |

| 3 | `client` 只做协议转换，业务规则在 service | 换调用方就得复制一遍重试/轮询/落库逻辑 |

| 4 | 错误翻译只有一份实现(`RagApiException.fromUpstream`) | 两条路各写一份，早晚一条腐烂，而它偏偏是排查时唯一的线索 |

| 5 | \*\*落库是旁路\*\*：所有落库走 `RagService.safeWrite` / `settleAsync` | DB 抖动变成用户的 500 → 诱导重传 → 同一份文档入库两次(上游没有幂等键) |



\*\*三个「唯一」\*\*：`RagService` 是唯一的业务编排场所；`RagController` 是唯一的 HTTP 入口、`GlobalExceptionHandler` 是唯一的异常出口；`RagClient` 是唯一碰上游 HTTP 的地方。



\*\*两条链路的同步/异步分界\*\*(不能挪)：



\- 链路 A(上传 + 轮询)全程同步，`ingest\_task` 一行随轮询\*\*就地更新\*\*；

\- 链路 B(流式问答)在请求线程上\*\*同步写\*\*开头的三条痕迹(会话行 + user 行 + 空 assistant 行)，之后的一切都是流的一部分，终态用 `doFinally` 甩到 `boundedElastic` 异步落库。把开头那次写库放进 `Flux.defer` 里，落库就成了流的一部分，DB 一抖会把用户的回答带崩。



> ⚠️ 本项目\*\*验证靠「真实启动 + 看日志 + Navicat」，不靠单元测试\*\*。测试用的 `computer\_biz\_test` 库单独一份，是因为启动自检会把 `status='STREAMING'` 的行全改成 `INTERRUPTED` —— 这条对着开发库跑是破坏性的。



\## 踩过的坑



\### Java BFF 侧



Spring Boot 4 / MyBatis-Plus 3.5.17 上抄教程最容易翻车的四点，`pom.xml` 和代码注释里都写明了：



1\. \*\*`RestClient` / `WebClient.Builder` 要单独加 starter\*\* —— Boot 4 把各 HTTP 客户端的自动配置拆成了独立模块(本项目要用 `spring-boot-starter-restclient` 和 `spring-boot-starter-webclient`)，光有 `starter-webflux` 拿不到 `WebClient.Builder`；

2\. \*\*MyBatis-Plus 必须是 `mybatis-plus-spring-boot4-starter`\*\* —— 同版本的 `-spring-boot3-starter` 在 Boot 4 上会以一堆 `NoClassDefFoundError` 收场；

3\. \*\*`ServiceImpl` 的包名变了\*\* —— `com.baomidou.mybatisplus.spring.service.impl.ServiceImpl`，老路径 `extension.service.impl` 在 3.5.17 里是空的；

4\. \*\*Jackson 升到 3(`tools.jackson` 包)\*\*，但 DTO 上仍用 Jackson 2 的注解(`com.fasterxml.jackson.annotation.\*`)。



另外两个实体层的坑(症状都是「不报错但结果不对」)：



\- 实体\*\*不要加 `@Builder` / `@AllArgsConstructor`\*\* —— MyBatis 靠「无参构造 + setter」组装对象，加了全参构造就没有默认构造，查出来的对象字段全是 null 且不报错；

\- 时间列\*\*不加 `@TableField(fill=...)`\*\* —— 值交给数据库的 `DEFAULT CURRENT\_TIMESTAMP(3)`(可行的原因是 MP 默认字段策略是 NOT\_NULL，值为 null 的字段压根不进 INSERT)。



\### Python 上游侧



\- \*\*入库耗时主要来自「切块 + 逐块调 LLM 抽取实体/关系」\*\*，属正常现象；进度可通过 `/v1/kb/ingest/{job\_id}` 查看；

\- \*\*提问返回空结果时\*\*，多为提示词超出模型上下文上限，可调小 `LIGHTRAG\_TOP\_K`、`LIGHTRAG\_MAX\_HISTORY\_TURNS`；

\- \*\*服务必须单 worker\*\* —— 依赖常驻事件循环与进程内单例，多 worker 会并发写同一工作目录。



\## 目前的边界(诚实清单)



| 项 | 状态 |

|---|---|

| 问答落库链路 | ✅ 已实测跑通(`chat\_message` 有真实数据) |

| \*\*上传/轮询链路\*\* | ⚠️ \*\*一次都没跑过\*\* —— `ingest\_task` 0 行，终态一次性、`poll\_count` 自增、404→UNKNOWN 都还是\*\*未验证\*\* |

| 客户端断开以哪种形态到达 | ⚠️ 未实测，`CANCEL` 和 `ClientAbortException` 两种都处理了 |

| 鉴权 | ❌ 无登录。`user\_id` 恒为常量 1，\*\*绝不信任请求里带的 `userId`\*\*(那是白送的越权) |

| 读接口(历史会话查询) | ❌ 本轮只写不读，验证靠 Navicat + 日志 |

| 上游 HTTP 超时 | ⚠️ 两个客户端都没设超时 |

| 空壳/未使用的类 | `FileUploadReq`(空占位)、`TaskQueryReq`(未使用)、`AiChatResp`(空且未使用) |

| `vo/` 包 | ⬜ 空目录，留给「要返回给前端的形状」 |



\## 相关文档



`docs/` 下的文档比这份 README 深得多，遇到具体问题优先翻它们：



| 文档 | 讲什么 |

|---|---|

| `项目整体说明.md` | 包结构与依赖关系，每个包的职责 + 为什么这么分 |

| `流式问答链路详解.md` | 字节流解码器 + 前端增量分帧，逐行代码 + 面试追问 |

| `rag对接实现说明.md` | 上游对接与联调时的坑(三个 SSE 相关事故的原始记录) |

| `落库设计说明.md` | 四张表的设计、巡检 SQL |

| `落库改动清单.md` | 78 条逐条编号的改动说明(每条带理由 + 验证方法) |

| `前端工程说明.md` | 前端四个 SSE 坑、store 设计、状态机 |

| `MyBatis 理解.md` | MyBatis / MyBatis-Plus 的概念笔记 |

| `start.md`(模块根目录) | 启动手册，含四个进程的完整命令 |



> ⚠️ 已知文档过时之处：`start.md` 第 ① 步写的上游路径 `D:\\python\\code` 已失效，现在在 `D:\\python\\LightRAG`；`docs/项目整体说明.md` 说前端「无 UI 框架、手写 CSS」，实际已经用了 Ant Design 5(`frontend/package.json`)。



