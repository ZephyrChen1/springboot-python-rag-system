"""新闻抓取进程的配置。

为什么单独一份，而不是塞进 config_data.py：
  - 这些选项只属于独立爬虫进程，FastAPI 永远不读；
  - 单测需要注入一份假的 env mapping，不能污染全局配置；
  - 密钥/模型仍然复用 config_data，避免两处维护。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# LightRAG 根目录：.../LightRAG/news_ingest/config.py -> .../LightRAG
ROOT = Path(__file__).resolve().parents[1]

# 先按包位置加载 .env，再 import config_data。
# 这样即使从别的 cwd 启动（例如任务计划程序），也能读到密钥。
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - python-dotenv 已在 requirements 中
    pass

try:
    import config_data as _shared
except Exception:  # pragma: no cover - 仅用于异常环境下的可读报错
    _shared = None


def _get(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    return default if value is None or value == "" else str(value)


def _get_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(_get(env, name, str(default)))
    except ValueError:
        return default


def _get_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(_get(env, name, str(default)))
    except ValueError:
        return default


def _get_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _get(env, name, "1" if default else "0").strip().lower()
    return value in ("1", "true", "yes", "on")


def _get_path(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


@dataclass(frozen=True)
class NewsConfig:
    root: Path
    api_base_url: str
    api_key: str
    llm_api_key: str
    llm_base_url: str
    sources_file: Path
    state_db: Path
    run_log_dir: Path
    lock_file: Path
    user_agent: str
    http_timeout_s: float
    feed_max_bytes: int
    article_max_bytes: int
    http_retries: int
    host_min_interval_s: float
    max_redirects: int
    respect_robots: bool
    max_feed_items: int
    max_articles_per_run: int
    max_per_source_per_run: int
    since_days: int
    run_max_minutes: int
    min_body_chars: int
    feed_full_content_min_chars: int
    fetch_article: bool
    llm_body_max_chars: int
    doc_body_max_chars: int
    doc_include_body: bool
    importance_threshold: int
    llm_model: str
    llm_timeout_s: float
    llm_max_tokens: int
    llm_temperature: float
    poll_interval_s: float
    job_timeout_s: float
    conflict_wait_s: float
    conflict_max_wait_s: float
    retry_max: int
    retry_base_s: float
    retry_max_s: float
    reingest_on_change: bool
    interval_minutes: int
    log_level: str


def load_config(env: Mapping[str, str] | None = None) -> NewsConfig:
    env = os.environ if env is None else env

    shared_api_key = str(getattr(_shared, "rag_api_key", "") or "")
    shared_llm_key = str(getattr(_shared, "API_KEY", "") or "")
    shared_base_url = str(
        getattr(_shared, "BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        or ""
    )
    shared_model = str(getattr(_shared, "LLM_MODEL", "qwen-plus") or "qwen-plus")

    return NewsConfig(
        root=ROOT,
        api_base_url=_get(env, "NEWS_API_BASE_URL", "http://127.0.0.1:8102").rstrip("/"),
        api_key=env.get("NEWS_API_KEY") or shared_api_key,
        llm_api_key=env.get("NEWS_LLM_API_KEY") or shared_llm_key,
        llm_base_url=env.get("NEWS_LLM_BASE_URL") or shared_base_url,
        sources_file=_get_path(env, "NEWS_SOURCES_FILE", ROOT / "news_ingest" / "sources.json"),
        state_db=_get_path(env, "NEWS_STATE_DB", ROOT / "crawler_state" / "news.db"),
        run_log_dir=_get_path(env, "NEWS_RUN_LOG_DIR", ROOT / "crawler_state" / "runs"),
        lock_file=_get_path(env, "NEWS_LOCK_FILE", ROOT / "crawler_state" / "news_ingest.lock"),
        user_agent=_get(env, "NEWS_USER_AGENT", "LightRAG-NewsIngest/1.0"),
        http_timeout_s=_get_float(env, "NEWS_HTTP_TIMEOUT_S", 20.0),
        feed_max_bytes=_get_int(env, "NEWS_FEED_MAX_BYTES", 2 * 1024 * 1024),
        article_max_bytes=_get_int(env, "NEWS_ARTICLE_MAX_BYTES", 5 * 1024 * 1024),
        http_retries=_get_int(env, "NEWS_HTTP_RETRIES", 2),
        host_min_interval_s=_get_float(env, "NEWS_HOST_MIN_INTERVAL_S", 3.0),
        max_redirects=_get_int(env, "NEWS_MAX_REDIRECTS", 5),
        respect_robots=_get_bool(env, "NEWS_RESPECT_ROBOTS", True),
        max_feed_items=_get_int(env, "NEWS_MAX_FEED_ITEMS", 50),
        # 0 = 不限：每轮处理所有到期文章
        max_articles_per_run=_get_int(env, "NEWS_MAX_ARTICLES_PER_RUN", 0),
        max_per_source_per_run=_get_int(env, "NEWS_MAX_PER_SOURCE_PER_RUN", 0),
        since_days=_get_int(env, "NEWS_SINCE_DAYS", 7),
        run_max_minutes=_get_int(env, "NEWS_RUN_MAX_MINUTES", 120),
        min_body_chars=_get_int(env, "NEWS_MIN_BODY_CHARS", 400),
        feed_full_content_min_chars=_get_int(env, "NEWS_FEED_FULL_CONTENT_MIN_CHARS", 2000),
        fetch_article=_get_bool(env, "NEWS_FETCH_ARTICLE", True),
        llm_body_max_chars=_get_int(env, "NEWS_LLM_BODY_MAX_CHARS", 6000),
        doc_body_max_chars=_get_int(env, "NEWS_DOC_BODY_MAX_CHARS", 20000),
        doc_include_body=_get_bool(env, "NEWS_DOC_INCLUDE_BODY", True),
        importance_threshold=_get_int(env, "NEWS_IMPORTANCE_THRESHOLD", 3),
        llm_model=_get(env, "NEWS_LLM_MODEL", shared_model),
        llm_timeout_s=_get_float(env, "NEWS_LLM_TIMEOUT_S", 120.0),
        llm_max_tokens=_get_int(env, "NEWS_LLM_MAX_TOKENS", 1200),
        llm_temperature=_get_float(env, "NEWS_LLM_TEMPERATURE", 0.1),
        poll_interval_s=_get_float(env, "NEWS_POLL_INTERVAL_S", 3.0),
        job_timeout_s=_get_float(env, "NEWS_JOB_TIMEOUT_S", 1800.0),
        conflict_wait_s=_get_float(env, "NEWS_CONFLICT_WAIT_S", 30.0),
        conflict_max_wait_s=_get_float(env, "NEWS_CONFLICT_MAX_WAIT_S", 900.0),
        retry_max=_get_int(env, "NEWS_RETRY_MAX", 5),
        retry_base_s=_get_float(env, "NEWS_RETRY_BASE_S", 300.0),
        retry_max_s=_get_float(env, "NEWS_RETRY_MAX_S", 86400.0),
        reingest_on_change=_get_bool(env, "NEWS_REINGEST_ON_CHANGE", False),
        interval_minutes=_get_int(env, "NEWS_INTERVAL_MINUTES", 360),
        log_level=_get(env, "NEWS_LOG_LEVEL", "INFO").upper(),
    )
