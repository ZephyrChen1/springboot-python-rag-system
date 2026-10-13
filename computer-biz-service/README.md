# computer-biz-service

企业知识库 + 大模型问答平台的 **Java BFF**(Backend For Frontend)。

用户上传文档 → 后台异步入库(切片、向量化、抽实体关系)→ 基于知识库做流式问答(RAG 检索增强生成),
答案逐字吐给前端。本模块是三个进程中间的那一层:对接上游 RAG 服务、把业务记录落进 MySQL、
把上游的错误翻译成前端认识的形状。

## 架构

```
浏览器 (React 18 + Vite, :5173)
      │  /api/rag/*
      ▼
Java BFF (Spring Boot 4, :8080)          ← 本模块
      │  /ai/chat  /v1/kb/*
      ▼
Python RAG 服务 (FastAPI + LightRAG, :8102)   ← 源码在仓库外的 D:\python\LightRAG
      │
      ├── MySQL 8.4  (:3306)   业务数据(用户/任务/会话/消息)
      └── Qdrant     (:6333)   向量库(Java 不直接碰)
```

**Java 这一层不是多余的中转**,它承担四件事:

1. **协议适配** —— 上游流式响应是非标准 SSE(裸文本),浏览器 `EventSource` 吃不下,
   需要一层把字节流翻译成前端能用的分帧格式;
2. **数据落库** —— 上游不留痕(答案只流一次、job 只保留最近一个),业务记录只能在这一层攒;
3. **统一错误契约** —— 把上游的 422 校验错误、404、502 都翻译成 `{"detail":"..."}`;
4. **对前端隐藏上游** —— 前端只认 `/api/rag/*`,上游换端口/换实现不影响前端。

## 技术栈

| 层 | 技术 | 备注 |
|---|---|---|
| BFF | Spring Boot 4.1.1、Java 21 | Boot 4 的坑见 [§ 踩过的坑](#踩过的坑) |
| Web | Spring MVC(servlet, Tomcat) + **WebFlux 的 WebClient** | MVC 当服务端,WebClient 只当流式客户端 |
| ORM | MyBatis-Plus 3.5.17(`mybatis-plus-spring-boot4-starter`) | 版本必须对上 Boot 4 |
| 数据库 | MySQL 8.4(严格模式) | utf8mb4 / InnoDB |
| 前端 | React 18 + Vite 7 + Ant Design 5 | `frontend/`,:5173 |
| 上游 | FastAPI + LightRAG + Qdrant + DashScope Qwen | 向量库、LLM 都在 Python 侧 |

> `pom.xml` 里同时有 `starter-webmvc` 和 `starter-webflux`,但这是个 **servlet 应用**。
> WebFlux 在这里只用来提供 `WebClient`,它的服务端自动配置被
> `@ConditionalOnWebApplication(REACTIVE)` 挡住了,不会生效。

## 快速开始

### 前置:MySQL 8.4

一次性建库、建账号(Navicat 里对 root 连接执行,密码自己定,别用 root 的):

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

然后建表:把 `db/schema.sql` 全文在 `computer_biz` 跑一遍,在 `computer_biz_test` 再跑一遍。
(本轮没有迁移工具,以后改表要改这个文件 + 在库上手写一条 `ALTER`。)

### 启动

四个进程按顺序起:MySQL → FastAPI(:8102) → Java(:8080) → Vite(:5173)。

```bash
# ① 上游 FastAPI(源码在仓库外的 D:\python\LightRAG)
uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1

# ② 本模块:密码不进仓库,每个新终端先设一次
export DB_PASSWORD=你的密码          # PowerShell: $env:DB_PASSWORD = "你的密码"
./mvnw spring-boot:run              # Windows: .\mvnw.cmd spring-boot:run

# ③ 前端
cd frontend
npm install                          # 只有第一次要
npm run dev
```

启动 ② 后,日志里按顺序应该出现:`HikariPool-1 - Start completed.` → 落库自检 ① → 落库自检 ②。
卡在哪一步就说明问题在哪一层,详见 `docs/落库设计说明.md`。

### 常用命令

```bash
./mvnw -B compile      # 只编译,最快的一道检查,不用数据库
./mvnw -B test         # 需要 computer_biz_test 库
./mvnw spring-boot:run "-Dspring-boot.run.arguments=--debug"   # 打自动配置报告,排查「bean 没装配上」
```

### 配置

数据库连接全部走占位符,默认值指向本机 `computer_biz` 库:

| 环境变量 | 默认值 |
|---|---|
| `DB_HOST` | `localhost` |
| `DB_PORT` | `3306` |
| `DB_NAME` | `computer_biz` |
| `DB_USER` | `computer_biz` |
| `DB_PASSWORD` | 空(必须自己设) |

上游地址在 `application.yaml` 的 `rag.base-url`,默认 `http://localhost:8102`。
单文件上传上限已从默认 1MB 调到 **100MB**(知识库文档)。

> ⚠️ 本项目**验证靠「真实启动 + 看日志 + Navicat」,不靠单元测试**。
> 测试用的 `computer_biz_test` 库单独一份,是因为启动自检会把 `status='STREAMING'` 的行
> 全改成 `INTERRUPTED` —— 这条对着开发库跑是破坏性的。

## 接口

三个接口都在 `/api/rag` 前缀下,与上游 FastAPI 的 `/ai/chat`、`/v1/kb/*` 是**两套独立命名**,
Java 可以自由重组 API 形状,不用跟着上游改。

| 方法 | 路径 | 入参 | 返回 |
|---|---|---|---|
| POST | `/api/rag/documents` | `multipart/form-data`(文件 + 额外参数) | `FileUploadResp`(含 `job_id`) |
| GET | `/api/rag/tasks/{jobId}` | 路径变量 | `TaskQueryResp` |
| POST | `/api/rag/chat` | `AiChatReq`(JSON) | `Flux<String>`,SSE 流 |

`controller` 里没有任何业务逻辑——不查库、不调上游、不判状态,薄到只剩转发。
业务规则集中在 `service`。

## 包结构

根包 `org.example.computerbizservice`,依赖方向**单向**:
`controller → service → client/mapper`。

```
├── controller/    RagController              唯一的 HTTP 入口
├── service/       业务编排(项目的中枢)
│   ├── RagService             主链路编排:调上游 + 织入落库;唯一同时持有 client 和两个 record service 的类
│   ├── IngestRecordService    入库任务的落库    → ingest_task(就地更新)
│   └── ChatRecordService      问答记录的落库    → chat_session + chat_message(追加)
├── client/        RagClient                  唯一碰上游 HTTP 的地方,只做协议转换
├── config/        RagClientConfig / RagClientProperties   装配两个 HTTP 客户端 Bean
├── dto/           request/rag、response/rag   纯数据,无逻辑
├── entity/        User / IngestTask / ChatSession / ChatMessage
├── mapper/        4 个**故意为空**的接口(extends BaseMapper 已够用)
├── common/        AppConstants、enums、PersistenceStartupCheck(启动自检)
├── util/          TextUtils
├── exception/     RagApiException、GlobalExceptionHandler(唯一的异常出口)
└── vo/            ⬜ 空目录,留给「要返回给前端的形状」
```

`RagClient` 用两个客户端:`RestClient`(同步)跑上传和轮询,`WebClient`(流式)跑问答。
问答那条路里最重的是自研的 `decodeRawText()` 字节流解码器,绕开 Spring 的 SSE 解析器、
自己处理 UTF-8 汉字跨块截断——详见 `docs/流式问答链路详解.md`。

## 设计要点

**五条铁律**:

| # | 规则 | 违反了会怎样 |
|---|---|---|
| 1 | 依赖单向:`controller → service → client/mapper` | 循环依赖启动报错,或分层失效改一处崩一片 |
| 2 | `entity` 不出 service 层 | `password_hash` 被序列化给前端 = 事故 |
| 3 | `client` 只做协议转换,业务规则在 service | 换调用方就得复制一遍重试/轮询/落库逻辑 |
| 4 | 错误翻译只有一份实现(`RagApiException.fromUpstream`) | 两条路各写一份,早晚一条腐烂,而它偏偏是排查时唯一的线索 |
| 5 | **落库是旁路**:所有落库走 `RagService.safeWrite` / `settleAsync` | DB 抖动变成用户的 500 → 诱导重传 → 同一份文档入库两次(上游没有幂等键) |

**三个「唯一」**:`RagService` 是唯一的业务编排场所;`RagController` 是唯一的 HTTP 入口、
`GlobalExceptionHandler` 是唯一的异常出口;`RagClient` 是唯一碰上游 HTTP 的地方。

**两条链路的同步/异步分界**(不能挪):

- 链路 A(上传 + 轮询)全程同步,`ingest_task` 一行随轮询**就地更新**;
- 链路 B(流式问答)在请求线程上**同步写**开头的三条痕迹(会话行 + user 行 + 空 assistant 行),
  之后的一切都是流的一部分,终态用 `doFinally` 甩到 `boundedElastic` 异步落库。
  把开头那次写库放进 `Flux.defer` 里,落库就成了流的一部分,DB 一抖会把用户的回答带崩。

## 数据库

四张表(`db/schema.sql`):

| 表 | 写入方 | 形态 |
|---|---|---|
| `user` | `PersistenceStartupCheck`(启动补写) | 1 行演示数据,**无登录** |
| `ingest_task` | `IngestRecordService` | 一行一任务,就地更新(上游返回的是快照不是事件流) |
| `chat_session` | `ChatRecordService.ensureSession` | 按需创建,无外键约束 |
| `chat_message` | `ChatRecordService` | 一次问答**两行**(user + assistant) |

三个反直觉的点:

- **同一轮问答的两行 `created_at` 完全相同** —— 一轮之内的先后只能靠 `id` 排,不能靠时间;
- **清空 `error` 只能写空串,不能写 null** —— MP 默认不把 null 字段写进 UPDATE,
  写 null 等于「这列不动」;而 `lambdaUpdate().set(...)` 是显式写列(null 也会写进去),
  **两套语义正好相反**;
- 状态列存**大写枚举名字符串**(`'DONE'`),不是数字;`ChatRole` 是唯一用小写的枚举
  (`'user'`/`'assistant'`),因为 role 的取值在接口和前端本来就是小写。

⚠️ 枚举值与前端状态机**一一对应,不要自己加第六个**:`ChatMsgStatus` 5 值 ↔ 前端 `chatStore.MSG`;
`IngestStatus` 5 值 ↔ 前端 `ingestStore.STATUS` 的后 5 个(前端多 3 个纯客户端状态
`queued`/`uploading`/`committing`,服务端永远看不到)。

## 踩过的坑

Spring Boot 4 / MyBatis-Plus 3.5.17 上抄教程最容易翻车的四点,`pom.xml` 和代码注释里都写明了:

1. **`RestClient` / `WebClient.Builder` 要单独加 starter** —— Boot 4 把各 HTTP 客户端的
   自动配置拆成了独立模块(本项目要用 `spring-boot-starter-restclient` 和
   `spring-boot-starter-webclient`),光有 `starter-webflux` 拿不到 `WebClient.Builder`;
2. **MyBatis-Plus 必须是 `mybatis-plus-spring-boot4-starter`** —— 同版本的
   `-spring-boot3-starter` 在 Boot 4 上会以一堆 `NoClassDefFoundError` 收场;
3. **`ServiceImpl` 的包名变了** —— `com.baomidou.mybatisplus.spring.service.impl.ServiceImpl`,
   老路径 `extension.service.impl` 在 3.5.17 里是空的;
4. **Jackson 升到 3(`tools.jackson` 包)**,但 DTO 上仍用 Jackson 2 的注解
   (`com.fasterxml.jackson.annotation.*`)。

另外两个实体层的坑(症状都是「不报错但结果不对」):

- 实体**不要加 `@Builder` / `@AllArgsConstructor`** —— MyBatis 靠「无参构造 + setter」组装对象,
  加了全参构造就没有默认构造,查出来的对象字段全是 null 且不报错;
- 时间列**不加 `@TableField(fill=...)`** —— 值交给数据库的 `DEFAULT CURRENT_TIMESTAMP(3)`
  (可行的原因是 MP 默认字段策略是 NOT_NULL,值为 null 的字段压根不进 INSERT)。

## 目前的边界(诚实清单)

| 项 | 状态 |
|---|---|
| 问答落库链路 | ✅ 已实测跑通(`chat_message` 有真实数据) |
| **上传/轮询链路** | ⚠️ **一次都没跑过** —— `ingest_task` 0 行,终态一次性、`poll_count` 自增、404→UNKNOWN 都还是**未验证** |
| 客户端断开以哪种形态到达 | ⚠️ 未实测,`CANCEL` 和 `ClientAbortException` 两种都处理了 |
| 鉴权 | ❌ 无登录。`user_id` 恒为常量 1,**绝不信任请求里带的 `userId`**(那是白送的越权) |
| 读接口(历史会话查询) | ❌ 本轮只写不读,验证靠 Navicat + 日志 |
| 上游 HTTP 超时 | ⚠️ 两个客户端都没设超时 |
| 空壳/未使用的类 | `FileUploadReq`(空占位)、`TaskQueryReq`(未使用)、`AiChatResp`(空且未使用) |
| `vo/` 包 | ⬜ 空目录,留给「要返回给前端的形状」 |

## 相关文档

`docs/` 下的文档比这份 README 深得多,遇到具体问题优先翻它们:

| 文档 | 讲什么 |
|---|---|
| `项目整体说明.md` | 包结构与依赖关系,每个包的职责 + 为什么这么分 |
| `流式问答链路详解.md` | 字节流解码器 + 前端增量分帧,逐行代码 + 面试追问 |
| `rag对接实现说明.md` | 上游对接与联调时的坑(三个 SSE 相关事故的原始记录) |
| `落库设计说明.md` | 四张表的设计、巡检 SQL |
| `落库改动清单.md` | 78 条逐条编号的改动说明(每条带理由 + 验证方法) |
| `前端工程说明.md` | 前端四个 SSE 坑、store 设计、状态机 |
| `MyBatis 理解.md` | MyBatis / MyBatis-Plus 的概念笔记 |
| `start.md`(模块根目录) | 启动手册,含四个进程的完整命令 |

> ⚠️ 已知文档过时之处:`start.md` 第 ① 步写的上游路径 `D:\python\code` 已失效,现在在
> `D:\python\LightRAG`;`docs/项目整体说明.md` 说前端「无 UI 框架、手写 CSS」,
> 实际已经用了 Ant Design 5(`frontend/package.json`)。
