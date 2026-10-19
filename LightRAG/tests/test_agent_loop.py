"""决策循环的离线测试。

不访问网络、不启动 Qdrant：模型调用和 LightRAG 都被替换成假实现。
目标是验证事件序列与工具执行的**行为契约**，不是验证模型效果。

运行：
    python -m unittest tests.test_agent_loop -v
"""

import asyncio
import json
import threading
import unittest
from unittest import mock

from agent import confirm, events, llm, loop, memory, runtime
from agent.tools import base as tools
from agent.tools import external
from agent.tools.base import ToolContext


# ---------------- 假模型返回值 ----------------

class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5


class FakeResp:
    def __init__(self, message):
        self.choices = [mock.Mock(message=message)]
        self.usage = FakeUsage()


def tool_call_resp(call_id, name, args):
    return FakeResp(FakeMessage(tool_calls=[FakeToolCall(call_id, name, json.dumps(args))]))


def answer_resp(text=""):
    return FakeResp(FakeMessage(content=text))


class FakeLightRAG:
    async def aquery_data(self, query, param=None):
        return {
            "status": "success",
            "message": "ok",
            "data": {
                "entities": [
                    {"entity_name": "报销", "entity_type": "流程", "description": "报销流程说明"}
                ],
                "relationships": [],
                "chunks": [
                    {"content": "报销需要发票。", "file_path": "制度.txt", "chunk_id": "c1"}
                ],
                "references": [{"reference_id": "1", "file_path": "制度.txt"}],
            },
            "metadata": {"query_mode": getattr(param, "mode", "hybrid")},
        }


def make_ctx(**overrides):
    data = dict(
        user_id=1,
        session_id="test_session",
        run_id="test_run",
        lightrag=FakeLightRAG(),
        allow_write=False,
        require_confirm=False,
    )
    data.update(overrides)
    return ToolContext(**data)


async def collect(question, ctx, history=None):
    return [event async for event in loop.run_agent(question, ctx, history or [])]


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        # 不让测试写真实的历史文件
        self.memory_patch = mock.patch.object(memory, "append_turn")
        self.memory_patch.start()

    def tearDown(self):
        self.memory_patch.stop()

    def test_direct_answer_does_not_call_tools(self):
        async def fake_plan(messages, schemas):
            return answer_resp("你好")

        async def fake_stream(messages, usage_sink=None):
            yield "你"
            yield "好"

        events = asyncio.run(self._run_with(fake_plan, fake_stream, "你好"))

        types = [e["type"] for e in events]
        self.assertEqual(types[0], "start")
        self.assertEqual(types[-1], "done")
        self.assertNotIn("tool_call", types)
        self.assertEqual("".join(e["delta"] for e in events if e["type"] == "answer"), "你好")
        memory.append_turn.assert_called_once()

    def test_tool_call_then_answer(self):
        calls = {"n": 0}

        async def fake_plan(messages, schemas):
            calls["n"] += 1
            if calls["n"] == 1:
                return tool_call_resp("c1", "kb_search", {"query": "报销", "mode": "hybrid"})
            return answer_resp("最终回答")

        async def fake_stream(messages, usage_sink=None):
            yield "最终回答"

        events = asyncio.run(self._run_with(fake_plan, fake_stream, "报销怎么走"))

        tool_calls = [e for e in events if e["type"] == "tool_call"]
        tool_results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "kb_search")
        self.assertTrue(tool_results[0]["ok"])
        self.assertIn("制度.txt", tool_results[0]["summary"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["steps"], 2)

    def test_unknown_tool_is_reported_not_crash(self):
        calls = {"n": 0}

        async def fake_plan(messages, schemas):
            calls["n"] += 1
            if calls["n"] == 1:
                return tool_call_resp("c1", "no_such_tool", {})
            return answer_resp("好的")

        async def fake_stream(messages, usage_sink=None):
            yield "好的"

        events = asyncio.run(self._run_with(fake_plan, fake_stream, "问题"))

        result = [e for e in events if e["type"] == "tool_result"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unknown_tool")
        self.assertEqual(events[-1]["type"], "done")

    def test_write_tool_blocked_when_disabled(self):
        calls = {"n": 0}

        async def fake_plan(messages, schemas):
            calls["n"] += 1
            if calls["n"] == 1:
                return tool_call_resp("c1", "kb_delete", {"file_name": "制度.txt"})
            return answer_resp("已取消")

        async def fake_stream(messages, usage_sink=None):
            yield "已取消"

        events = asyncio.run(
            self._run_with(fake_plan, fake_stream, "删掉制度.txt", make_ctx(allow_write=False))
        )

        result = [e for e in events if e["type"] == "tool_result"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "write_disabled")
        self.assertEqual(events[-1]["type"], "done")

    def test_tool_timeout_becomes_observation(self):
        # 用一个永远不返回的假工具替换 kb_search
        class SlowTool(tools.Tool):
            name = "kb_search"
            description = "slow"
            parameters = {"type": "object", "properties": {}}
            timeout_s = 0.05
            side_effect = False

            async def run(self, args, ctx):
                await asyncio.sleep(10)
                return tools.ToolResult(True, "never")

        calls = {"n": 0}

        async def fake_plan(messages, schemas):
            calls["n"] += 1
            if calls["n"] == 1:
                return tool_call_resp("c1", "kb_search", {"query": "x"})
            return answer_resp("超时了")

        async def fake_stream(messages, usage_sink=None):
            yield "超时了"

        original_get = tools.get
        tools.get = lambda name: SlowTool() if name == "kb_search" else original_get(name)
        try:
            events = asyncio.run(self._run_with(fake_plan, fake_stream, "问题"))
        finally:
            tools.get = original_get

        result = [e for e in events if e["type"] == "tool_result"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")
        self.assertEqual(events[-1]["type"], "done")

    async def _run_with(self, fake_plan, fake_stream, question, ctx=None):
        with mock.patch.object(llm, "plan", fake_plan), mock.patch.object(
            llm, "stream_answer", fake_stream
        ):
            return await collect(question, ctx or make_ctx())


class CalculatorSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tool = external.CalculatorTool()

    def _run(self, expression):
        return asyncio.run(self.tool.run({"expression": expression}, make_ctx()))

    def test_normal_expression(self):
        result = self._run("(23*17+5)/4")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["result"], 99)

    def test_rejects_function_call(self):
        result = self._run("__import__('os').system('echo hacked')")
        self.assertFalse(result.ok)

    def test_rejects_huge_power(self):
        result = self._run("9**9**9")
        self.assertFalse(result.ok)


class FakeService:
    """模拟 LightRAGService 的常驻 loop + submit。"""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro_factory, *args, **kwargs):
        return asyncio.run_coroutine_threadsafe(
            coro_factory(*args, **kwargs), self._loop
        )

    def close(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)


class RuntimeBridgeTest(unittest.TestCase):
    """验证同步门面：常驻 loop + 队列回传 + NDJSON 行协议。"""

    def test_stream_events_bridges_and_serializes(self):
        async def fake_plan(messages, schemas):
            return answer_resp("好的")

        async def fake_stream(messages, usage_sink=None):
            yield "好"
            yield "的"

        service = FakeService()
        try:
            with mock.patch.object(llm, "plan", fake_plan), mock.patch.object(
                llm, "stream_answer", fake_stream
            ), mock.patch.object(memory, "load", return_value=[]), mock.patch.object(
                memory, "append_turn"
            ), mock.patch.object(
                runtime.trace, "TraceWriter", return_value=None
            ):
                collected = list(runtime.stream_events(service, "问题", "s1", 1))
        finally:
            service.close()

        self.assertEqual(collected[0]["type"], "start")
        self.assertEqual(collected[-1]["type"], "done")
        self.assertEqual(
            "".join(e["delta"] for e in collected if e["type"] == "answer"), "好的"
        )

        # 行协议：每条事件恰好一行，且内容中的换行不会破坏协议
        for event in collected:
            line = events.to_line(event)
            self.assertTrue(line.endswith("\n"))
            self.assertEqual(line.count("\n"), 1)
            self.assertEqual(json.loads(line)["type"], event["type"])

    def test_multiline_answer_stays_on_one_line(self):
        line = events.to_line({"type": "answer", "delta": "第一行\n第二行"})
        self.assertEqual(line.count("\n"), 1)
        self.assertEqual(json.loads(line)["delta"], "第一行\n第二行")


class HttpEndpointTest(unittest.TestCase):
    """验证 /ai/agent/chat 真的吐出 NDJSON，而不是把 dict 当字符串。"""

    def test_chat_returns_ndjson(self):
        from fastapi.testclient import TestClient
        import config_data as config
        import rag_api.routers.ai_agent as ai_agent
        from rag_api.main import app

        async def fake_plan(messages, schemas):
            return answer_resp("你好")

        async def fake_stream(messages, usage_sink=None):
            yield "你"
            yield "好"

        service = FakeService()
        try:
            with mock.patch.object(ai_agent, "get_lightrag_service", return_value=service), \
                 mock.patch.object(llm, "plan", fake_plan), \
                 mock.patch.object(llm, "stream_answer", fake_stream), \
                 mock.patch.object(memory, "load", return_value=[]), \
                 mock.patch.object(memory, "append_turn"), \
                 mock.patch.object(runtime.trace, "TraceWriter", return_value=None), \
                 mock.patch.object(config, "rag_api_key", ""):
                response = TestClient(app).post(
                    "/ai/agent/chat",
                    json={"session_id": "s1", "message": "你好"},
                )
        finally:
            service.close()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/x-ndjson"))

        parsed = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        self.assertEqual(parsed[0]["type"], "start")
        self.assertEqual(parsed[-1]["type"], "done")
        self.assertEqual(
            "".join(e["delta"] for e in parsed if e["type"] == "answer"), "你好"
        )

    def test_rejects_bad_session_id(self):
        from fastapi.testclient import TestClient
        import config_data as config
        from rag_api.main import app

        with mock.patch.object(config, "rag_api_key", ""):
            response = TestClient(app).post(
                "/ai/agent/chat",
                json={"session_id": "../../etc/passwd", "message": "hi"},
            )
        self.assertEqual(response.status_code, 422)


class ConfirmTest(unittest.TestCase):
    def test_approve(self):
        pending = confirm.create("kb_delete", {"file_name": "x"}, ttl_s=5)
        self.assertTrue(confirm.resolve(pending.token, True))

        approved = asyncio.run(confirm.wait(pending))
        self.assertTrue(approved)

    def test_reject(self):
        pending = confirm.create("kb_delete", {"file_name": "x"}, ttl_s=5)
        self.assertTrue(confirm.resolve(pending.token, False))
        self.assertFalse(asyncio.run(confirm.wait(pending)))

    def test_expired(self):
        pending = confirm.create("kb_delete", {}, ttl_s=1)
        pending.expires_at = 0  # 直接过期，避免测试真的等
        self.assertFalse(asyncio.run(confirm.wait(pending)))


if __name__ == "__main__":
    unittest.main()
