# computer-biz-frontend

知识库文档入库 + 知识库问答的前端，与后端完全分离，可独立开发和部署。

## 链路

```
浏览器 ──/api/rag/*──► Java :8080 ──┬─/v1/kb/*──► FastAPI :8102   （入库，轮询）
                       (BFF 网关)    └─/ai/chat──► FastAPI :8102   （问答，SSE 流式）
```

浏览器**只跟 Java :8080 打交道**。开发期由 Vite dev server 把 `/api` 代理到 8080，
浏览器视角是同源的，**所以后端不需要任何 CORS 配置**。

> ⚠️ 生产环境没有 Vite 代理了。`npm run build` 出来的 `dist/` 是纯静态文件，
> 要么让 nginx 把 `/api` 反代到 8080，要么把 Java 和前端放同一个域。
> 分域部署时必须在 Java 侧加 CORS 配置。

## 跑起来

需要三个进程（各开一个终端）：

```bash
# ① FastAPI（端口 8102）
#    你的启动命令

# ② Java BFF（端口 8080）
cd /d/java/computer-biz-service && ./mvnw spring-boot:run

# ③ 前端（端口 5173）
cd /d/java/computer-biz-service/frontend && npm install && npm run dev
```

然后打开 http://localhost:5173

## 上游的硬限制（已实测确认，直接决定了前端设计）

对接的 FastAPI 侧有三条限制，改动前端前务必先读：

| 限制 | 表现 | 前端如何应对 |
| --- | --- | --- |
| **同时只允许一个入库任务** | 第二个文件被拒：`已有入库任务正在处理：「xxx.txt」，请稍后再试` | `ingestStore` 用串行闸门，上一个任务到达终态后才放出下一个 |
| **只保留最近一个 job** | 新任务一提交，旧 `job_id` 立刻 404：`任务不存在(仅保留最近一个任务)` | 不能「先全部上传再慢慢轮询」，必须一个一个来 |
| **只接受 `.txt`** | `不支持的文件格式: .md（支持 ['.txt']）` | `UploadPage` 的 `accept=".txt"` 与上游对齐 |

> 这三条都是**上游行为**，不是本前端的取舍。哪天 FastAPI 放开了，
> 改 `ingestStore.js` 的 `pump()` 和 `UploadPage.jsx` 的 `ACCEPT` 即可。

## 进度为什么分三段

上传是**两段式**的：`POST /api/rag/documents` 立即返回 `job_id`，
真正的入库是 FastAPI 在后台异步做的。所以 UI 上有三段：

| 阶段 | 状态 | 进度来源 |
| --- | --- | --- |
| A 上传字节 | `UPLOADING` | axios `onUploadProgress`，**有真实百分比** |
| A→B 空档 | `COMMITTING` | 字节发完了但服务端还没受理，通常不到 1 秒 |
| B 异步入库 | `INGESTING` | **没有百分比**，只有 `stage` 人类可读文案 + `elapsed_sec` |

阶段 B 刻意用**转圈 + 文案**而不是进度条：`stage` 是
`"实体/关系抽取中（3 个文本块）…"` 这种自然语言，后端不提供分子分母，
画成进度条只能靠编。而「填满的进度条卡在 100%」比转圈更像 bug。

## 问答为什么走 fetch 而不是 axios

上传和轮询都走 `api/http.js` 的 axios 实例，**只有问答（SSE）走 fetch**，
`api/rag.js` 的 `chatStream` 里手写分帧。原因不是偏好：

| 方案 | 为什么不行 |
| --- | --- |
| 浏览器的 `EventSource` | 只支持 GET，不能带自定义头、不能发 JSON 请求体，而问答是 POST |
| axios | 底层是 XHR，**浏览器里没有流式读的口子**。`onDownloadProgress` 给的是「累积到目前的整段文本」，拿它硬拼增量既笨又容易错 |
| `fetch` + `ReadableStream` | 可以，代价是错误翻译要自己做一遍 |

所以 `http.js` 把「抽 detail + 带 status」抽成了 `toHttpError()` 导出，
SSE 那条路复用它 —— **错误文案只能有一份实现**，否则早晚有一份先腐烂。

顺带记录了四个真实会踩的坑（都写在 `api/rag.js` 的注释里，也有对应的验证脚本）：

1. **Spring 的 `SseEmitter` 不转义内容里的换行**：一个多行文本块会被写成
   `data:第一行` + 换行 + `第二行`，而不是规范的 `data:第一行\ndata:第二行`。
   不处理的话 markdown 回答只剩第一行。
2. **上游失败时状态码已经改不了了**：Java 侧响应一旦以 200 提交，Spring 只能把
   错误 JSON **直接写进 SSE 流**（那些行没有 `data:` 前缀）。前端要能从这些
   「悬空行」里捞出 `detail`，否则用户看到的是「回答莫名其妙断了」。
3. **半个汉字**：一个汉字 3 字节，很容易被切在两个 chunk 之间。`TextDecoder` 必须
   带 `{ stream: true }`，否则满屏 ``。
4. **SSE 路径上的错误体是被协商掉的**（实测，最容易把人带偏）：浏览器发
   `Accept: text/event-stream`，接口的 `produces` 也是 `text/event-stream`，
   于是 `GlobalExceptionHandler` 想返回 `ApiErrorResp` 时**找不到能写这个对象的
   转换器** —— 5xx 变成「没有 Content-Type、body 为空」的裸响应：

   ```
   Accept: application/json   -> 406  {"detail":"Acceptable representations: [text/event-stream]."}
   Accept: text/event-stream  -> 500  ct=null  body=""
   Accept: */*                -> 500  {"detail":"服务内部错误"}
   ```

   空 body 的 5xx 在 axios 那条路上等价于「Java 没起来」，所以 `toHttpError`
   会给出「请确认后端已在 8080 启动」。**那句话在这个接口上是误导**：Java 明明活着，
   真实原因是它后面那步炸了。`chatStream` 因此单独改写了这条文案。

   后端一行就能治本（把错误体救回来，前端也就拿到了真正的原因）：

   ```java
   // GlobalExceptionHandler：ResponseEntity 上显式指定类型，
   // 这样就不再走 Accept 协商，ApiErrorResp 一定能写成 JSON
   return ResponseEntity.status(...)
           .contentType(MediaType.APPLICATION_JSON)
           .body(new ApiErrorResp(e.getDetail()));
   ```

## 错误处理

后端所有错误出口都是 `{"detail": "..."}` + 真实 HTTP 状态码，
`api/http.js` 的拦截器统一抽出来，并且**必须把 `status` 一起带出去** ——
轮询层要靠它区分「404 = 任务已失效，重试无意义」和「502 = 暂时故障，应该重试」。

**最关键的一条规则：轮询失败 ≠ 入库失败。**

任务只有 `done === true` 且 `result` 以 `[失败]` 开头才算真失败。
客户端侧的任何异常（网络抖动、502、超时）都只标 `UNKNOWN`（状态未知），
并保留已有的 `stage` 和耗时。否则一次上游重启就会让用户误以为入库失败而重传，
**导致同一文档入库两次**（后端没有幂等键）。

## 验证

```bash
# 串行入库 + 全流程（需三个进程都在跑）
node tools/verify-serial.cjs <文件1> <文件2> <文件3>

# 错误路径（需先停掉 Java :8080）
node tools/verify-error.cjs <文件>

# 问答链路：SSE 分帧 + 会话状态机（★ 不需要任何后端）
node tools/verify-chat.mjs

# 错误文案回归（★ 不需要任何后端）
node tools/verify-http-errors.mjs
```

`verify-serial.cjs` 会每隔 400ms 采样一次 DOM，断言**任一时刻活动态任务数 ≤ 1**，
并输出截图到 `tools/verify-serial.png`。

`verify-chat.mjs` 自己用 `ReadableStream` 造上游，专门回放那些界面表现为
「缺字 / 少一段 / 出错了却显示成功」的脏情况（半个汉字跨块、帧被切断、
不转义的换行、错误 JSON 混进流里、中途 abort）。这两个 bug 类看起来都像玄学，
跑一次就知道坏在哪一层。

## 目录

```
src/
├─ api/
│  ├─ http.js          axios 实例 + 错误拦截器（抽 detail、带 status）；
│  │                    toHttpError 是两条链路的公共出口
│  └─ rag.js           三个接口的薄封装 + snake_case 字段归一化；
│                      chatStream = fetch + 手写 SSE 分帧
├─ ingest/
│  └─ ingestStore.js   ★ 核心：状态机 + 串行闸门 + 轮询调度
├─ chat/
│  └─ chatStore.js     问答会话：消息列表 + 流式写入 + 停止/中断/失败收尾
│                      （刻意比 ingestStore 薄：不排队、不轮询）
├─ hooks/
│  ├─ useIngestTasks.js  只是 store 的订阅壳
│  └─ useChat.js         同上
├─ components/
│  ├─ TaskItem.jsx     单个任务行（三段式进度）
│  ├─ TaskList.jsx
│  └─ ChatMessage.jsx  一条消息气泡（纯文本，不做 markdown 渲染）
├─ pages/
│  ├─ UploadPage.jsx   Upload.Dragger 对接点
│  └─ ChatPage.jsx     输入框 + 消息列表（Enter 发送 / Shift+Enter 换行）
└─ utils/format.js
```

两个页面在 `App.jsx` 里用 antd `Tabs` 切换。两个 store 都是模块级单例，
所以切页签不会丢状态、也不会打断正在生成的回答。

## 上游 `/ai/chat` 的请求契约（已实测确认）

`user_id` 和 `top_k` 这两个字段之前是待确认项，现在都实测过了（`session_id` 也是）：

| 字段 | 上游要求 | 前端行为 |
| --- | --- | --- |
| `session_id` | **必填**，1~128 位，只允许字母/数字/下划线/连字符 | `chatStore.newSessionId()` 生成 |
| `message` | **必填**，1~20000 字 | 输入框内容（会 trim） |
| `user_id` | 可选，`int`，默认 `0`。**但显式传 `null` 会被 422 拒掉**（`Input should be a valid integer`） | `USER_ID = null` → 字段整个不发 ✓ |
| `top_k` | 可选，1~100 | 默认发 `5`（`DEFAULT_TOP_K`） |

> ⚠️ `user_id` 这条坑得很隐蔽：FastAPI 侧是 `user_id: int = 0`（**非 Optional**），
> 「不传」= 用默认值 0，但「传 null」= 类型错。所以 Java 的 DTO 必须
> `@JsonInclude(NON_NULL)`，否则 Jackson 会老实地把 `"user_id": null` 发出去。

## 一个已知的字段名风险

前端依赖 `job_id` / `file_name` / `elapsed_sec` 这些 **snake_case** 字段名，
它们靠 DTO 上的 `@JsonProperty` 维持。本项目已迁移到 **Jackson 3**
（`tools.jackson.databind`），而 Jackson 3 的注解包名仍是
`com.fasterxml.jackson.annotation`（官方为平滑迁移刻意保留），所以 `@JsonProperty` 照常生效
—— **已实测确认返回的确实是 snake_case**。

`api/rag.js` 里仍同时兼容 camelCase，作为零成本保险。
