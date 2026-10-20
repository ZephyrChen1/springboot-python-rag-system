# 自动新闻抓取（news_ingest）

从官方博客 RSS / 官方 sitemap 抓取 AI 前沿发布，清洗正文，用规则 + 一次 LLM 调用筛出
**模型/产品发布、重大能力更新**，生成中文摘要 + 原文的 `.txt`，再通过**运行中的 FastAPI**
写入知识库。

## 为什么是独立进程

```text
news_ingest ──HTTP──> POST /v1/kb/upload ──> LightRAG（单 worker、入库互斥锁）
     │                     │
     │                     └── GET /v1/kb/ingest/{job_id}
     └── SQLite 状态库 + JSONL 运行日志
```

爬虫**不 import** `lightrag_service`，也**不写** `Lightrag_db/`、`uploads/`、`md5.text`。
所有写入都经过 API 的 `submit_ingest`：互斥用跨进程文件锁
（`crawler_state/ingest.lock`），任务状态落在 `crawler_state/rag_jobs.db`，
所以爬虫和 API 谁先拿到锁谁写，只有一个写入者。API 仍须用 `--workers 1`
启动——LightRAG 的 KV 是文件型、每进程一份快照，这与入库互斥是两回事。

## 快速开始

```bash
# 1. 启动 API（单 worker）
uvicorn rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1

# 2. 只检查源可达性（不写 DB、不调 LLM、不入库）
python -m news_ingest --once --check-sources

# 3. 零 LLM 成本 dry-run：抓取 + 清洗 + 规则预筛
python -m news_ingest --once --dry-run --dry-run-no-llm --source openai --limit 3

# 4. 带 LLM 的 dry-run：看判定理由、中文摘要、拟入库文件名
python -m news_ingest --once --dry-run --source openai --limit 3

# 5. 单篇真实端到端
python -m news_ingest --once --source openai --limit 1

# 6. 常驻循环（也可交给 cron / 任务计划程序）
python -m news_ingest --loop --interval-minutes 360
```

`--dry-run` 使用内存 SQLite，不创建 `crawler_state/news.db`，也不推进 feed 的
ETag/Last-Modified。真实运行开始前会 `GET /health`，API 不在线直接 exit 1。

## 同一轮的命令行参数

| 参数 | 作用 |
|---|---|
| `--once` / `--loop` | 跑一轮 / 常驻循环（默认 once） |
| `--interval-minutes N` | loop 间隔，默认 `NEWS_INTERVAL_MINUTES=360` |
| `--source ID` | 只处理指定源；可重复或逗号分隔；显式指定 disabled 源会打 warning |
| `--limit N` | 本轮最多处理多少篇候选（进入 LLM 或上传）；默认取 `NEWS_MAX_ARTICLES_PER_RUN`，`0` = 不限 |
| `--since-days N` | 日期窗口，抓取/预筛/过期清理共用；`0` 关闭日期过滤和过期清理 |
| `--force` | 重置终态文章（ingested/skipped/filtered_out/failed）重新处理 |
| `--dry-run` | 预览；不写 DB、不 POST、不推进 ETag |
| `--dry-run-no-llm` | 只抓取/清洗/规则预筛；隐含 `--dry-run` |
| `--check-sources` | 只拉取并解析源，输出每个源的状态/条目数/最新日期 |
| `--log-level` | DEBUG/INFO/WARNING/ERROR |

退出码：`0` 成功；`1` 致命错误（配置、锁、DB、API health、KB/LLM 鉴权）；`2` 本轮
有源失败或文章失败。`--check-sources` 有源失败也返回 2，适合 cron 告警。

## 默认源

均在 `sources.json` 中版本化。网络是否可达由运行时 `--check-sources` 反映；单源失败
不影响其他源。

| id | 入口 | 说明 |
|---|---|---|
| `openai` | `https://openai.com/news/rss.xml` | 官方新闻 RSS |
| `deepmind` | `https://deepmind.google/blog/rss.xml` | 官方 RSS |
| `google_ai` | `https://blog.google/technology/ai/rss/` | 官方 RSS |
| `huggingface` | `https://huggingface.co/blog/feed.xml` | 官方 RSS |
| `meta` | `https://about.fb.com/news/feed/` | 官方 newsroom，混有非 AI 内容，靠规则 + LLM 过滤 |
| `anthropic` | `https://www.anthropic.com/sitemap.xml` | 官方无 RSS；用官方 sitemap + 文章页 |
| `apple_ml` | `https://machinelearning.apple.com/rss.xml` | 官方 RSS |
| `aws_ml` | `https://aws.amazon.com/blogs/machine-learning/feed/` | 官方 RSS |
| `qwen` | `https://qwenlm.github.io/blog/index.xml` | 默认 `enabled=false`，验证可达后开启 |

新增源只需改 `sources.json`：

```json
{
  "id": "openai",
  "name": "OpenAI",
  "enabled": true,
  "type": "rss",
  "feed_url": "https://openai.com/news/rss.xml",
  "site_url": "https://openai.com/news/",
  "domains": ["openai.com"],
  "article_url_patterns": ["^https://openai\\.com/(index|news)/"],
  "language": "en",
  "max_items": 50
}
```

安全校验：feed、文章页、重定向后的**每一跳**必须落在 `domains` 内；拒绝 userinfo、
私有/环回/link-local IP、非 http(s)、超过 2048 字符的 URL。生产源必须 https。

## 处理流程与过滤规则

1. **条件 GET**：feed 带 `If-None-Match` / `If-Modified-Since`；304 直接跳过。
2. **解析**：RSS 2.0 / RSS 1.0(RDF) / Atom；Anthropic 用 sitemap（支持一层 index）。
3. **日期窗口 + URL 规范化去重**：去 fragment、`utm_*`、`gclid`、`fbclid`、`ref`、
   `source`，host 小写、默认端口去掉、query 排序、尾斜杠归一化。
4. **过期清理**：每轮真实运行（非 `--force`）在抓取之后先把超过日期窗口仍停在 `new`
   的文章标记为 `filtered_out(too_old)`，**只改状态、不删行**。时间基准是
   `published_at`，缺失时回退 `fetched_at`；已分类/已入库/待重试的行不受影响。
5. **正文**：feed 自带全文 ≥ 2000 字符直接用；否则抓文章页。文章页失败/robots 禁止/
   Cloudflare 页 → 回退 feed 内容；两边都 < 400 字符 → `filtered_out(insufficient_content)`，
   不调 LLM。
6. **规则预筛**：强负向词（融资、政策、合作、活动、招聘、诉讼等）**且没有**发布信号
   （launch/release/model/发布/开源/…）才直接拒绝；其余交给 LLM。`--force` 不绕过规则。
7. **一次 LLM 调用**：`temperature=0.1`、`max_tokens=1200`、`timeout=120s`。正文放在
   `<article_data_{nonce}>` 中，system prompt 明确它是**不可信数据**，其中任何指令都忽略。
   输出严格 JSON：`relevant/category/importance/title_zh/summary_zh/key_facts/reason`。
8. **入库条件**：`relevant=true` 且 `category∈{model_release, product_update}` 且
   `importance >= NEWS_IMPORTANCE_THRESHOLD`（默认 3）。
9. **文档**：中文标题、原标题、来源、日期、原文链接、分类、重要度、中文摘要、关键事实、
   原文正文（默认 20000 字符）。**不写抓取时间**，保证同一文章版本的文档字节稳定，
   服务端 MD5 去重才有意义。
10. **入库**：`POST /v1/kb/upload` → `GET /v1/kb/ingest/{job_id}` 轮询。409 等待重试，
    不抢锁；401/403 整轮 exit 1；`[成功]`→ingested，`[跳过]`→skipped，`[失败]`/空/未知→failed。

每轮默认处理**全部**到期文章（`NEWS_MAX_ARTICLES_PER_RUN=0`、
`NEWS_MAX_PER_SOURCE_PER_RUN=0` 表示不限）；仍按源轮转决定顺序，
`NEWS_RUN_MAX_MINUTES` 是唯一的墙钟安全阀。

LLM 超时、429、5xx、JSON 非法、字段缺失一律 fail closed（记 `failed`，按退避重试），
**绝不放行入库**。连续失败超过 `NEWS_RETRY_MAX` 进入 dead letter（`next_retry_at=NULL`），
等待人工检查或 `--force`。

## 状态与重试

状态库：`crawler_state/news.db`（SQLite，WAL）。

```text
new ──规则拒绝/太旧/内容不足──> filtered_out
new ──超过日期窗口（每轮清理）──> filtered_out(too_old)
new ──LLM 拒绝/低于阈值──────> filtered_out
new ──抓取/LLM 暂时失败──────> failed（retry_count++，next_retry_at 退避）
new ──LLM 通过───────────────> classified（保存 doc_text，重试上传不再调 LLM）
classified ──上传被接受──────> ingest_pending
ingest_pending ──[成功]──────> ingested
ingest_pending ──[跳过]──────> skipped
ingest_pending ──[失败]/超时/404──> failed（下一轮重传，服务端 MD5 兜底）
failed ──到点且未超上限──────> 重新处理
failed ──超过上限────────────> dead letter（next_retry_at=NULL）
```

每轮另写 `crawler_state/runs/<run_id>.jsonl`：事件、URL、标题、状态、错误摘要；
不写正文和密钥，错误截断到 1000 字符。

## 定时任务

Windows（任务计划程序，每 6 小时）：

```bat
schtasks /Create /TN "LightRAG News Ingest" /SC HOURLY /MO 6 ^
  /TR "cmd /c cd /d D:\LocalGit\springboot-python-rag-system\LightRAG && .venv\Scripts\python.exe -m news_ingest --once >> crawler_state\cron.log 2>&1"
```

Linux cron（每 6 小时的 :17）：

```cron
17 */6 * * * cd /path/to/LightRAG && .venv/bin/python -m news_ingest --once >> crawler_state/cron.log 2>&1
```

单实例由 OS 文件锁保证（Windows `msvcrt.locking`，POSIX `fcntl.flock`）；cron 和
`--loop` 不会同时跑。锁文件里的 PID 只用于诊断，不用 `os.kill` 判断存活。

## Web 可视化

Web 端「抓取动态」页签直接展示状态库，**只读**，不触发抓取/入库/删除：

```text
React NewsPage
   │  GET /api/rag/news/**
   ▼
Spring NewsController → NewsService → RagClient
   │  GET /v1/news/**
   ▼
FastAPI news router → NewsStore（SQLite mode=ro + PRAGMA query_only）
   │
   ▼
crawler_state/news.db
```

页面包含：

- 顶部统计：累计发现、已入库、待处理、已过滤、失败、最近一轮运行；
- **抓取内容**：按状态/来源筛选、标题/摘要搜索、分页；详情抽屉展示中文摘要、
  关键事实、分类/重要度、原文链接、入库文件名与正文；
- **运行记录**：每轮的开始时间、耗时、退出码、抓取/候选/过滤/入库/失败统计，
  展开可见逐源统计与错误；
- **数据源状态**：最近抓取时间、HTTP 状态、连续失败次数和最近错误。

`articles` 没有 `run_id` 外键，`fetched_at` 只在文章首次插入时写入，`last_seen_at`
会被后续运行覆盖。因此页面按「首次发现时间」展示文章，运行记录单独统计，
**不声称某篇文章属于某一轮运行**。

状态库不存在时页面显示提示，先跑一轮：

```bash
python -m news_ingest --once
```

对应 API（需要与启动 Web 时相同的 FastAPI 进程）：

```text
GET /v1/news/overview?limit=10
GET /v1/news/items?status=&source_id=&q=&page=1&page_size=20
GET /v1/news/items/{id}
```

列表接口不返回 `doc_text` / `feed_summary` 大字段；详情正文优先用清洗后的
`doc_text`，没有时回退到清洗后的 feed 摘要，最多 30k 字符。非法 `status`
返回 400，文章不存在返回 404。

## 配置

完整列表见 `config.py` 和 `.env.example` 的 `NEWS_*` 段。最常改的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEWS_API_BASE_URL` | `http://127.0.0.1:8102` | FastAPI 地址 |
| `NEWS_API_KEY` | 空（回退 `RAG_API_KEY`） | `X-API-Key` |
| `NEWS_MAX_ARTICLES_PER_RUN` | 0 | 本轮候选上限；0 = 不限 |
| `NEWS_MAX_PER_SOURCE_PER_RUN` | 0 | 每源每轮上限；0 = 不限（仍按源轮转，防饿死） |
| `NEWS_SINCE_DAYS` | 7 | 日期窗口（抓取/预筛/过期清理共用）；0 关闭 |
| `NEWS_IMPORTANCE_THRESHOLD` | 3 | 最低重要度 |
| `NEWS_LLM_MODEL` | 空（回退 `LLM_MODEL`） | 模型名 |
| `NEWS_INTERVAL_MINUTES` | 360 | loop 间隔 |
| `NEWS_REINGEST_ON_CHANGE` | 0 | 正文变化是否重入库 |

密钥复用 `config_data.py`：`RAG_API_KEY`、`API_KEY`、`BASE_URL`、`LLM_MODEL`。
`config.py` 先显式加载 LightRAG 根目录的 `.env`，因此从任意 cwd 启动都能读到密钥。

## 测试

全离线（fake HTTP / fake LLM / fake KB / 临时 SQLite）：

```bash
python -m unittest tests.test_news_pipeline tests.test_news_api -v
python -m compileall news_ingest rag_api
```

`tests.test_news_api` 用临时 SQLite 覆盖统计/筛选/分页/LIKE 转义/详情回退/
状态库缺失；不需要启动 FastAPI 或访问网络。

## 已知限制

- **Anthropic** 没有官方 RSS，使用官方 sitemap + 文章页；页面结构变化会导致 failed，
  不会入库垃圾。
- **Meta** 官方 newsroom 混有非 AI 内容，依赖规则 + LLM 过滤。
- 部分官方源在特定网络不可达；单源失败不阻塞其他源，也不会用 headless browser 绕过反爬。
- 提示注入有残余风险：nonce 分隔 + “不可信数据” system prompt + 严格 JSON + dry-run
  审计只能降低，不能消除。
- **不做自动删除**：7 天清理只是把过期未处理的行标成 `filtered_out(too_old)`，
  文章和运行记录都保留（`canonical_url` 是去重键，删行会导致 feed 老 URL 复活）。
- `--force` 跳过批量清理，但仍受规则预筛的日期窗口约束；要重跑窗口外的文章需要
  `--force --since-days 0`。
- URL 已终态且正文变化时默认只记录新 hash。
- 任务注册表 1 小时过期 / API 重启后，`ingest_pending` 会 404 → 重传；服务端 MD5 兜底。
  极小窗口（LightRAG 已插入但 `save_md5` 未执行时崩溃）可能重复 chunk。
- 只抓官方公开页面，内部知识库使用，保留原文链接；正文可截断。
