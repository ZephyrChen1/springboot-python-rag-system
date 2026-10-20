"""同步、一次性的 LLM completion。

刻意使用 openai.OpenAI（同步）而不是 agent/llm.py 的 AsyncOpenAI：
爬虫是独立进程，没有 LightRAG 的常驻事件循环；同步客户端也不会
把 agent 的依赖带进来。失败一律由调用方 fail closed。
"""

from __future__ import annotations

from typing import Any

from .config import NewsConfig


class LLMError(RuntimeError):
    pass


class LLMFatalError(LLMError):
    """配置/鉴权错误：整轮应终止，不要重试。"""


_clients: dict[tuple[str, str, float], Any] = {}


def _client(cfg: NewsConfig) -> Any:
    if not cfg.llm_api_key:
        raise LLMFatalError(
            "未配置 LLM 密钥：请设置 NEWS_LLM_API_KEY 或 DASHSCOPE_API_KEY/API_KEY"
        )
    key = (cfg.llm_api_key, cfg.llm_base_url, cfg.llm_timeout_s)
    if key not in _clients:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - requirements 已声明
            raise LLMFatalError("未安装 openai，请先 pip install -r requirements.txt") from exc
        _clients[key] = OpenAI(
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url or None,
            timeout=cfg.llm_timeout_s,
            max_retries=0,
        )
    return _clients[key]


def complete(
    messages: list[dict[str, str]],
    *,
    cfg: NewsConfig,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 1200,
    timeout_s: float | None = None,
) -> str:
    try:
        response = _client(cfg).chat.completions.create(
            model=model or cfg.llm_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout_s or cfg.llm_timeout_s,
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__
        if status in (401, 403) or name in (
            "AuthenticationError",
            "PermissionDeniedError",
        ):
            raise LLMFatalError(f"LLM 鉴权失败（HTTP {status}）") from exc
        raise LLMError(f"LLM 调用失败：{name}: {exc}") from exc
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMError("LLM 返回结构异常") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMError("LLM 返回空内容")
    return content
