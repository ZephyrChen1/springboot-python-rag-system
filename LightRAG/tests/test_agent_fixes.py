"""针对 agentic 改造复查问题的回归测试。

覆盖：
  - 历史消息截断（单条 + 总轮数）
  - trace 不再记录逐 token 的 answer 事件
  - tool_result 带实测 elapsed_ms
  - done.usage 累计 planning + 最终流式回答
  - 用户拒绝后的相同写操作不再弹确认、也不执行
  - 确认等待被取消时清理 pending token
  - 断连时真正取消常驻 loop 上的 agent Task
  - 暂存文件按文件名解析
  - 入库任务完成一段时间内仍可按 job_id 查询
  - 删除文档后清理 md5 去重索引
  - kb_status 查指定文件时不会回退成「别的文件的最新记录」
  - 写操作预检失败时不弹确认框

运行：
    python -m unittest tests.test_agent_fixes -v
"""

import asyncio
import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import config_data as config
from agent import confirm, llm, memory, runtime
from agent.tools import base as tools
from agent.tools import kb as kb_tools
from knowledge_base import (
    check_md5,
    forget_md5,
    read_ingest_progress,
    save_md5,
)
from rag_api import ingest, staging
from tests.test_agent_loop import (
    FakeUsage,
    answer_resp,
    make_ctx,
    tool_call_resp,
    collect,
)


def answer_stream(*parts, usage=None):
    """构造一个与 llm.stream_answer 同签名的假流式生成器。"""

    async def fake_stream(messages, usage_sink=None):
        if usage is not None and usage_sink is not None:
            usage_sink["usage"] = usage
        for part in parts:
            yield part

    return fake_stream


class MemoryTruncationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(memory, "_DIR", self.tmp.name),
            mock.patch.object(config, "agent_history_max_chars", 50),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_append_and_load_truncate_each_message(self):
        memory.append_turn("s1", "问" * 200, "答" * 200)

        loaded = memory.load("s1")
        self.assertEqual(len(loaded), 2)
        self.assertTrue(all(len(m["content"]) <= 50 for m in loaded))

        with open(os.path.join(self.tmp.name, "agent_s1.json"), encoding="utf-8") as f:
            raw = json.load(f)
        self.assertTrue(all(len(m["content"]) <= 50 for m in raw))

    def test_load_truncates_legacy_long_messages(self):
        path = os.path.join(self.tmp.name, "agent_legacy.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                [{"role": "user", "content": "x" * 500}],
                f,
                ensure_ascii=False,
            )

        loaded = memory.load("legacy")
        self.assertEqual(len(loaded), 1)
        self.assertLessEqual(len(loaded[0]["content"]), 50)


class TraceAndUsageTest(unittest.TestCase):
    def test_trace_skips_answer_tokens(self):
        class TraceSpy:
            def __init__(self):
                self.events = []

            def write(self, event):
                self.events.append(event)

        async def fake_plan(messages, schemas):
            return answer_resp("你好")

        spy = TraceSpy()
        with mock.patch.object(llm, "plan", fake_plan), \
             mock.patch.object(llm, "stream_answer", answer_stream("你", "好")), \
             mock.patch.object(memory, "append_turn"):
            events_out = asyncio.run(collect("你好", make_ctx(trace=spy)))

        types = [e["type"] for e in events_out]
        self.assertEqual(types.count("answer"), 2)
        trace_types = [e["type"] for e in spy.events]
        self.assertNotIn("answer", trace_types)
        self.assertIn("start", trace_types)
        self.assertIn("done", trace_types)

    def test_tool_result_has_elapsed_and_usage_accumulates(self):
        calls = {"n": 0}

        async def fake_plan(messages, schemas):
            calls["n"] += 1
            if calls["n"] == 1:
                return tool_call_resp("c1", "kb_search", {"query": "报销"})
            return answer_resp("最终回答")

        with mock.patch.object(llm, "plan", fake_plan), \
             mock.patch.object(
                 llm, "stream_answer", answer_stream("最终回答", usage=FakeUsage())
             ), \
             mock.patch.object(memory, "append_turn"):
            out = asyncio.run(collect("报销怎么走", make_ctx()))

        result = [e for e in out if e["type"] == "tool_result"][0]
        self.assertIn("elapsed_ms", result)
        self.assertGreaterEqual(result["elapsed_ms"], 0)

        # 两次 planning（工具决策 + 最终回答，各 10/5）+ 一次流式回答（10/5）
        done = out[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["usage"]["prompt_tokens"], 30)
        self.assertEqual(done["usage"]["completion_tokens"], 15)


class RejectedWriteTest(unittest.TestCase):
    def test_same_rejected_write_is_not_confirmed_twice(self):
        calls = {"n": 0}

        async def fake_plan(messages, schemas):
            calls["n"] += 1
            if calls["n"] <= 2:
                return tool_call_resp(f"c{calls['n']}", "kb_delete", {"file_name": "制度.txt"})
            return answer_resp("已取消删除")

        pending = types.SimpleNamespace(token="t", expires_at=time.time() + 60)

        with mock.patch.object(llm, "plan", fake_plan), \
             mock.patch.object(llm, "stream_answer", answer_stream("已取消删除")), \
             mock.patch.object(memory, "append_turn"), \
             mock.patch.object(kb_tools, "_find_doc_id", return_value=("doc1", {}, 1)), \
             mock.patch.object(confirm, "create", return_value=pending), \
             mock.patch.object(confirm, "wait", new=mock.AsyncMock(return_value=False)):
            out = asyncio.run(
                collect(
                    "删掉制度.txt",
                    make_ctx(allow_write=True, require_confirm=True),
                )
            )

        self.assertEqual(
            len([e for e in out if e["type"] == "confirm_required"]), 1
        )
        results = [e for e in out if e["type"] == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(e["error"] == "not_confirmed" for e in results))
        self.assertIn("不要重试", results[1]["summary"])
        self.assertEqual(out[-1]["type"], "done")


class DisabledAgentEndpointTest(unittest.TestCase):
    def test_disabled_agent_returns_structured_error_not_404(self):
        from fastapi.testclient import TestClient
        from rag_api.main import app

        with mock.patch.object(config, "agent_enabled", False), \
             mock.patch.object(config, "rag_api_key", ""):
            response = TestClient(app).post(
                "/ai/agent/chat",
                json={"session_id": "s1", "message": "你好"},
            )

        self.assertEqual(response.status_code, 200)
        parsed = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["type"], "error")
        self.assertEqual(parsed[0]["code"], "AGENT_DISABLED")


class ConfirmCancelTest(unittest.TestCase):
    def test_cancelled_wait_removes_pending_token(self):
        pending = confirm.create("kb_delete", {}, ttl_s=30)

        async def scenario():
            task = asyncio.create_task(confirm.wait(pending))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertNotIn(pending.token, confirm._PENDING)


class CancelAwareService:
    """FakeService + 线程安全的 cancel_task。"""

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

    def cancel_task(self, task):
        self._loop.call_soon_threadsafe(task.cancel)

    def close(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)


class RuntimeCancelTest(unittest.TestCase):
    def test_disconnect_cancels_loop_task(self):
        started = threading.Event()
        cancelled = threading.Event()

        async def fake_plan(messages, schemas):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        service = CancelAwareService()
        try:
            with mock.patch.object(llm, "plan", fake_plan), \
                 mock.patch.object(memory, "load", return_value=[]), \
                 mock.patch.object(memory, "append_turn"), \
                 mock.patch.object(runtime.trace, "TraceWriter", return_value=None):
                gen = runtime.stream_events(service, "问题", "s1", 1)
                self.assertEqual(next(gen)["type"], "start")
                self.assertTrue(started.wait(2))
                gen.close()
                self.assertTrue(cancelled.wait(2), "断连后 agent Task 没有被取消")
        finally:
            service.close()


class StagingFindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(config, "agent_stage_dir", self.tmp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_find_latest_staged_by_name(self):
        first = staging.save_staged("制度.txt", b"v1")
        time.sleep(0.05)
        second = staging.save_staged("制度.txt", b"v2")
        staging.save_staged("其他.txt", b"other")

        found = staging.find_staged_by_name("制度.txt")
        self.assertIsNotNone(found)
        self.assertEqual(found["staged_id"], second["staged_id"])
        self.assertNotEqual(found["staged_id"], first["staged_id"])
        self.assertIsNone(staging.find_staged_by_name("不存在.txt"))

    def test_expired_staged_files_are_cleaned_on_next_save(self):
        old = staging.save_staged("old.txt", b"x")
        old_time = time.time() - 25 * 3600
        for ext in (".json", ".bin"):
            os.utime(
                os.path.join(self.tmp.name, old["staged_id"] + ext),
                (old_time, old_time),
            )

        staging.save_staged("new.txt", b"y")
        self.assertIsNone(staging.find_staged_by_name("old.txt"))


class IngestJobRegistryTest(unittest.TestCase):
    class FakeKB:
        def upload_file(self, filename, content):
            time.sleep(0.05)
            return f"[成功]{filename}"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(ingest, "KnowledgeBaseService", self.FakeKB),
            mock.patch.object(ingest, "read_ingest_progress", return_value=None),
            mock.patch.object(
                config, "ingest_job_db", os.path.join(self.tmp.name, "jobs.db")
            ),
            mock.patch.object(
                config, "ingest_lock_path", os.path.join(self.tmp.name, "ingest.lock")
            ),
        ]
        for p in self.patches:
            p.start()
        with ingest._lock:
            ingest._active_job_id = None
            ingest._active_lock = None

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_finished_job_stays_queryable_after_next_submit(self):
        first = ingest.submit_ingest("first.txt", b"1")
        with self.assertRaises(ingest.IngestBusyError):
            ingest.submit_ingest("busy.txt", b"x")

        done = asyncio.run(ingest.wait_job(first["job_id"], timeout=5))
        self.assertTrue(done["done"])

        second = ingest.submit_ingest("second.txt", b"2")
        asyncio.run(ingest.wait_job(second["job_id"], timeout=5))

        # 关键回归：第二个任务跑完后，第一个任务仍然查得到
        self.assertIsNotNone(ingest.get_job(first["job_id"]))
        self.assertIsNotNone(ingest.get_job(second["job_id"]))


class Md5IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "md5.text")
        self.patch = mock.patch.object(config, "md5_path", self.path)
        self.patch.start()
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("aaa\tone.txt\n")
            f.write("bbb\n")  # 旧格式：只有 hash

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_filename_column_does_not_poison_check(self):
        self.assertTrue(check_md5("aaa"))
        self.assertTrue(check_md5("bbb"))
        self.assertFalse(check_md5("one.txt"))

    def test_forget_by_filename_and_content_hash(self):
        self.assertEqual(forget_md5(file_name="dir/one.txt"), 1)
        self.assertFalse(check_md5("aaa"))
        self.assertTrue(check_md5("bbb"))

        self.assertEqual(forget_md5(content_hash="bbb"), 1)
        self.assertFalse(check_md5("bbb"))

        save_md5("ccc", "dir/three.txt")
        with open(self.path, encoding="utf-8") as f:
            saved = f.read()
        self.assertIn("ccc\t", saved)
        self.assertIn("three.txt", saved)
        # 按 basename 匹配，所以传不带目录的文件名也能清掉
        self.assertEqual(forget_md5(file_name="three.txt"), 1)
        self.assertFalse(check_md5("ccc"))


class IngestProgressLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "kv_store_doc_status.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "d1": {
                        "file_path": "dir/memcheck.txt",
                        "status": "processed",
                        "chunks_count": 1,
                        "updated_at": "2026-01-01T00:00:00",
                    },
                    "d2": {
                        "file_path": "another.txt",
                        "status": "processing",
                        "chunks_count": 3,
                        "updated_at": "2026-02-01T00:00:00",
                    },
                },
                f,
                ensure_ascii=False,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_does_not_fall_back_to_latest(self):
        # 关键回归：查一个不存在的文件，绝不能把 memcheck/another 的状态返回
        self.assertIsNone(read_ingest_progress(self.tmp.name, "设计文档.txt"))

    def test_matches_by_basename(self):
        row = read_ingest_progress(self.tmp.name, "memcheck.txt")
        self.assertIsNotNone(row)
        self.assertEqual(row["file_path"], "dir/memcheck.txt")

    def test_no_filter_returns_latest(self):
        row = read_ingest_progress(self.tmp.name)
        self.assertEqual(row["file_path"], "another.txt")


class KbStatusLookupTest(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.TemporaryDirectory()
        self.stagedir = tempfile.TemporaryDirectory()
        with open(
            os.path.join(self.workdir.name, "kv_store_doc_status.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                {
                    "d1": {
                        "file_path": "memcheck.txt",
                        "status": "processed",
                        "chunks_count": 1,
                        "updated_at": "2026-01-01T00:00:00",
                    }
                },
                f,
            )
        self.patches = [
            mock.patch.object(kb_tools.config, "lightrag_working_dir", self.workdir.name),
            mock.patch.object(kb_tools.config, "agent_stage_dir", self.stagedir.name),
            mock.patch.object(kb_tools, "find_job_by_file_name", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.workdir.cleanup()
        self.stagedir.cleanup()

    def test_unknown_file_is_reported_as_not_found(self):
        result = asyncio.run(
            kb_tools.KbStatusTool().run({"file_name": "设计文档.txt"}, make_ctx())
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_found")
        self.assertIn("设计文档.txt", result.summary)
        self.assertNotIn("memcheck.txt", result.summary)

    def test_staged_file_is_reported_as_waiting(self):
        staging.save_staged("设计文档.txt", b"hello")
        result = asyncio.run(
            kb_tools.KbStatusTool().run({"file_name": "设计文档.txt"}, make_ctx())
        )
        self.assertTrue(result.ok)
        self.assertIn("暂存区", result.summary)
        self.assertIn("kb_ingest", result.summary)

    def test_no_argument_returns_latest_but_says_so(self):
        result = asyncio.run(kb_tools.KbStatusTool().run({}, make_ctx()))
        self.assertTrue(result.ok)
        self.assertIn("未指定文件", result.summary)


class WritePrecheckTest(unittest.TestCase):
    def _run(self, tool_name, args):
        calls = {"n": 0}

        async def fake_plan(messages, schemas):
            calls["n"] += 1
            if calls["n"] == 1:
                return tool_call_resp("c1", tool_name, args)
            return answer_resp("先上传文件")

        with mock.patch.object(llm, "plan", fake_plan), \
             mock.patch.object(llm, "stream_answer", answer_stream("先上传文件")), \
             mock.patch.object(memory, "append_turn"):
            return asyncio.run(
                collect(
                    "处理文件",
                    make_ctx(allow_write=True, require_confirm=True),
                )
            )

    def test_ingest_missing_staged_file_skips_confirmation(self):
        with mock.patch.object(kb_tools, "find_staged_by_name", return_value=None):
            out = self._run("kb_ingest", {"file_name": "设计文档.txt"})

        self.assertEqual([e for e in out if e["type"] == "confirm_required"], [])
        result = [e for e in out if e["type"] == "tool_result"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_found")
        self.assertIn("上传待入库文件", result["summary"])

    def test_delete_missing_document_skips_confirmation(self):
        with mock.patch.object(kb_tools, "_find_doc_id", return_value=(None, None, 0)):
            out = self._run("kb_delete", {"file_name": "不存在.txt"})

        self.assertEqual([e for e in out if e["type"] == "confirm_required"], [])
        result = [e for e in out if e["type"] == "tool_result"][0]
        self.assertEqual(result["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
