"""Agent 使用的 LLM 客户端。

为什么不用 lightrag.llm.openai.openai_complete_if_cache：
它的返回类型是 str，会把 tool_calls 拍平成文本。决策循环需要结构化的
tool_calls，所以直接用 lightrag 暴露的 create_openai_async_client 拿原始
AsyncOpenAI 客户端。

DashScope 兼容模式支持 OpenAI 的 function calling 协议。
"""

import config_data as config
from lightrag.llm.openai import create_openai_async_client

_client = None


def get_client():
    global _client
    if _client is None:
        _client = create_openai_async_client(
            api_key=config.API_KEY,
            base_url=config.BASE_URL,
        )
    return _client


async def plan(messages: list[dict], tools: list[dict] | None):
    """规划调用：非流式，带 tools。用于决定「继续调工具」还是「可以回答了」。"""
    kwargs = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    return await get_client().chat.completions.create(**kwargs)


async def stream_answer(messages: list[dict], usage_sink: dict | None = None):
    """最终回答：流式，不带 tools。

    usage_sink：可选的 dict。上游若在流里返回 usage（部分兼容实现会在
    最后一个 chunk 带上），就写进 usage_sink["usage"]。没有也不影响输出。
    """
    stream = await get_client().chat.completions.create(
        model=config.LLM_MODEL,
        messages=messages,
        stream=True,
        temperature=0.2,
    )
    async for chunk in stream:
        usage = getattr(chunk, "usage", None)
        if usage is not None and usage_sink is not None:
            usage_sink["usage"] = usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content
