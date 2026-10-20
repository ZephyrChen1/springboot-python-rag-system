"""入库任务注册表 / 跨进程写锁 / 会话历史并发写入的回归测试。

这些能力是「进程内状态外置」的基础：
  - 任务状态落 SQLite，API 重启或换进程仍能查到；
  - KB 写锁是 OS 文件锁，同一时刻只有一个入库/删除；
  - 同一 session 的历史文件是整文件读-改-写，必须串行。

运行：
    python -m unittest tests.test_ingest_jobs -v
"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest import mock

from langchain_core.messages import HumanMessage

import config_data as config
from agent import memory
from file_history_store import FileChatMessageHistory
from news_ingest.lock import LockBusy, RunLock
from rag_api import ingest, jobs


class JobRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jobs.db")
        self.patches = [
            mock.patch.object(config, "ingest_job_db", self.db),
            mock.patch.object(config, "ingest_lock_path", os.path.join(self.tmp.name, "ingest.lock")),
            mock.patch.object(ingest, "read_ingest_progress", return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_create_get_finish_is_visible_from_another_connection(self):
        job = jobs.create_job("制度.txt")
        self.assertFalse(job["done"])

        # 每次调用都是新连接，模拟「另一个进程」查询
        found = jobs.get_job(job["job_id"])
        self.assertEqual(found["file_name"], "制度.txt")
        self.assertFalse(found["done"])
        self.assertEqual(jobs.latest_running()["job_id"], job["job_id"])

        jobs.finish_job(job["job_id"], "[成功]完成")

        done = jobs.get_job(job["job_id"])
        self.assertTrue(done["done"])
        self.assertEqual(done["result"], "[成功]完成")
        self.assertIsNotNone(done["finished_at"])
        self.assertIsNone(jobs.latest_running())

    def test_find_latest_by_basename(self):
        first = jobs.create_job("dir/制度.txt")
        jobs.finish_job(first["job_id"], "[成功]v1")
        time.sleep(0.01)
        second = jobs.create_job("C:\\tmp\\制度.txt")
        jobs.finish_job(second["job_id"], "[成功]v2")
        other = jobs.create_job("其他.txt")
        jobs.finish_job(other["job_id"], "[成功]other")

        found = jobs.find_latest_by_file_name("制度.txt")
        self.assertEqual(found["job_id"], second["job_id"])
        self.assertIsNone(jobs.find_latest_by_file_name("不存在.txt"))
        self.assertIsNone(jobs.find_latest_by_file_name(""))

    def test_fail_stale_running_marks_leftovers(self):
        stale = jobs.create_job("中断.txt")
        self.assertEqual(jobs.fail_stale_running(), 1)
        row = jobs.get_job(stale["job_id"])
        self.assertTrue(row["done"])
        self.assertIn("中断", row["result"])
        # 已经没有 running 行了，再调一次返回 0
        self.assertEqual(jobs.fail_stale_running(), 0)

    def test_prune_keeps_latest_done_jobs_only(self):
        ids = []
        for i in range(25):
            job = jobs.create_job(f"f{i}.txt")
            jobs.finish_job(job["job_id"], f"[成功]{i}")
            ids.append(job["job_id"])

        # 显式写入有序时间戳，避免依赖系统时钟分辨率
        base = time.time() - 1000
        with closing(sqlite3.connect(self.db)) as conn:
            for i, job_id in enumerate(ids):
                conn.execute(
                    "UPDATE ingest_jobs SET finished_at=? WHERE job_id=?",
                    (base + i, job_id),
                )
            conn.commit()

        running = jobs.create_job("current.txt")

        self.assertIsNone(jobs.get_job(ids[0]))
        self.assertIsNotNone(jobs.get_job(ids[-1]))
        self.assertIsNotNone(jobs.get_job(running["job_id"]))

        with closing(sqlite3.connect(self.db)) as conn:
            done = conn.execute(
                "SELECT COUNT(*) FROM ingest_jobs WHERE status='done'"
            ).fetchone()[0]
        self.assertEqual(done, jobs.JOBS_MAX_KEPT)

    def test_crashed_running_job_is_reclaimed_on_read(self):
        # 模拟上次进程在入库中途死掉：库里有 running 行，但写锁是空的
        job = jobs.create_job("crash.txt")

        reported = ingest.get_job(job["job_id"])

        self.assertTrue(reported["done"])
        self.assertIn("中断", reported["result"])

    def test_live_running_job_is_not_reclaimed(self):
        with ingest._lock:
            ingest._active_job_id = None
            ingest._active_lock = None
        holder = RunLock(os.path.join(self.tmp.name, "ingest.lock")).acquire()
        try:
            job = jobs.create_job("live.txt")
            reported = ingest.get_job(job["job_id"])
            self.assertFalse(reported["done"])
        finally:
            holder.release()
            jobs.finish_job(job["job_id"], "[成功]done")

    def test_concurrent_readers_during_finish(self):
        job = jobs.create_job("a.txt")
        errors = []

        def reader():
            deadline = time.time() + 2
            while time.time() < deadline:
                try:
                    jobs.get_job(job["job_id"])
                except Exception as exc:  # database is locked 等
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(10):
            jobs.finish_job(job["job_id"], "[成功]done")
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(jobs.get_job(job["job_id"])["done"])

    def test_prune_drops_done_jobs_past_ttl(self):
        job = jobs.create_job("old.txt")
        jobs.finish_job(job["job_id"], "[成功]old")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE ingest_jobs SET finished_at=? WHERE job_id=?",
                (time.time() - jobs.JOB_TTL_S - 60, job["job_id"]),
            )
            conn.commit()

        jobs.create_job("new.txt")
        self.assertIsNone(jobs.get_job(job["job_id"]))


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_acquire_fails_and_succeeds_after_release(self):
        first = RunLock(self.path).acquire()
        try:
            with self.assertRaises(LockBusy):
                RunLock(self.path).acquire()
        finally:
            first.release()

        second = RunLock(self.path).acquire()
        second.release()

    def test_timeout_waits_for_release(self):
        first = RunLock(self.path).acquire()

        def release_later():
            time.sleep(0.2)
            first.release()

        threading.Thread(target=release_later, daemon=True).start()
        started = time.monotonic()
        second = RunLock(self.path).acquire(timeout=2.0)
        waited = time.monotonic() - started
        second.release()

        self.assertGreaterEqual(waited, 0.15)

    def test_timeout_expires(self):
        first = RunLock(self.path).acquire()
        try:
            started = time.monotonic()
            with self.assertRaises(LockBusy):
                RunLock(self.path).acquire(timeout=0.15)
            self.assertGreaterEqual(time.monotonic() - started, 0.1)
        finally:
            first.release()

    def test_other_process_cannot_take_held_lock(self):
        """真正的跨进程验证：子进程拿不到父进程持有的锁。"""
        code = (
            "import sys\n"
            "from news_ingest.lock import LockBusy, RunLock\n"
            "try:\n"
            "    RunLock(sys.argv[1]).acquire()\n"
            "except LockBusy:\n"
            "    print('BUSY')\n"
            "else:\n"
            "    print('ACQUIRED')\n"
        )

        def child() -> str:
            proc = subprocess.run(
                [sys.executable, "-c", code, self.path],
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        held = RunLock(self.path).acquire()
        try:
            self.assertEqual(child(), "BUSY")
        finally:
            held.release()

        self.assertEqual(child(), "ACQUIRED")


class IngestOtherProcessBusyTest(unittest.TestCase):
    """别人拿着文件锁时，即使本进程没有 _active_job_id 也要快速报忙。"""

    class FakeKB:
        def upload_file(self, filename, content):
            return f"[成功]{filename}"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_path = os.path.join(self.tmp.name, "ingest.lock")
        self.patches = [
            mock.patch.object(ingest, "KnowledgeBaseService", self.FakeKB),
            mock.patch.object(config, "ingest_job_db", os.path.join(self.tmp.name, "jobs.db")),
            mock.patch.object(config, "ingest_lock_path", self.lock_path),
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

    def test_submit_reports_busy_when_lock_held_elsewhere(self):
        holder = RunLock(self.lock_path).acquire()
        try:
            with self.assertRaises(ingest.IngestBusyError):
                ingest.submit_ingest("x.txt", b"x")
        finally:
            holder.release()

        # 锁释放后应能正常提交
        job = ingest.submit_ingest("x.txt", b"x")
        done = asyncio.run(ingest.wait_job(job["job_id"], timeout=5))
        self.assertTrue(done["done"])


class ChatHistoryConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_writes_keep_every_message(self):
        history = FileChatMessageHistory("s1", storage_path=self.tmp.name)

        def writer(start: int):
            for i in range(5):
                history.add_messages([HumanMessage(content=f"{start}-{i}")])

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(history.messages), 50)

    def test_concurrent_agent_turns_are_not_lost(self):
        # agent 的 append_turn 也是整文件读-改-写
        with mock.patch.object(memory, "_DIR", self.tmp.name), \
             mock.patch.object(config, "agent_history_max_turns", 100):
            def writer(n: int):
                for i in range(3):
                    memory.append_turn("s1", f"q{n}-{i}", f"a{n}-{i}")

            threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            self.assertEqual(len(memory._read_all("s1")), 48)

    def test_different_sessions_do_not_block_each_other(self):
        a = FileChatMessageHistory("a", storage_path=self.tmp.name)
        b = FileChatMessageHistory("b", storage_path=self.tmp.name)
        a.add_messages([HumanMessage(content="A")])
        b.add_messages([HumanMessage(content="B")])
        self.assertEqual([m.content for m in a.messages], ["A"])
        self.assertEqual([m.content for m in b.messages], ["B"])


if __name__ == "__main__":
    unittest.main()
