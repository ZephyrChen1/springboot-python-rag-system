"""rag_api.news_store 的离线单测：只读查询，不依赖真实 crawler_state。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from news_ingest.state import ArticleRecord, StateStore
from rag_api.news_store import NewsStore


class NewsStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "news.db"
        self.sources_file = self.tmp / "sources.json"
        self.sources_file.write_text(
            json.dumps(
                {
                    "sources": [
                        {"id": "aws_ml", "name": "AWS Machine Learning Blog", "enabled": True},
                        {"id": "openai", "name": "OpenAI Blog", "enabled": False},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.store = StateStore(self.db_path)
        self.store.close()  # NewsStore 只需要文件存在；写入用独立的连接

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    def _write(self, callback):
        with StateStore(self.db_path) as store:
            callback(store)

    @staticmethod
    def _article(
        store: StateStore,
        *,
        url: str,
        source_id: str,
        title: str,
        fetched_at: str | None = None,
        published_at: str | None = None,
    ) -> int:
        item_id, _ = store.upsert_article(
            ArticleRecord(
                canonical_url=url,
                source_id=source_id,
                guid=url,
                title=title,
                published_at=published_at or fetched_at or "2026-10-19T00:00:00Z",
            ),
            now=fetched_at or "2026-10-19T00:00:00Z",
        )
        return item_id

    def _news(self) -> NewsStore:
        return NewsStore(db_path=self.db_path, sources_file=self.sources_file)

    # ------------------------------------------------------------------
    def test_overview_counts_sources_and_runs(self) -> None:
        def seed(store: StateStore) -> None:
            first = self._article(
                store,
                url="https://example.com/a",
                source_id="aws_ml",
                title="Structured outputs",
                fetched_at="2026-10-19T08:00:00Z",
            )
            store.set_status(
                "https://example.com/a",
                "ingested",
                now="2026-10-19T09:00:00Z",
                title_zh="结构化输出",
                summary_zh="AWS 发布了结构化输出能力。",
                category="product_update",
                importance=4,
            )
            self._article(
                store,
                url="https://example.com/b",
                source_id="openai",
                title="New model",
                fetched_at="2026-10-19T10:00:00Z",
            )
            self._article(
                store,
                url="https://example.com/c",
                source_id="openai",
                title="Tutorial",
                fetched_at="2026-10-19T11:00:00Z",
            )
            store.set_status(
                "https://example.com/c",
                "filtered_out",
                now="2026-10-19T11:30:00Z",
                reason="教程，不是发布",
            )
            store.save_feed_state(
                "aws_ml", "https://example.com/aws.xml", status=200, now="2026-10-19T12:00:00Z"
            )
            store.save_feed_state(
                "openai",
                "https://example.com/openai.xml",
                status=503,
                error="upstream unavailable",
                now="2026-10-19T12:01:00Z",
            )
            # 状态库里出现过、但 sources.json 没注册的源
            store.save_feed_state(
                "legacy", "https://example.com/legacy.xml", status=200, now="2026-10-19T12:02:00Z"
            )

            store.record_run_start("run-1", "once", started_at="2026-10-19T12:00:00Z")
            store.record_run_source(
                "run-1",
                "aws_ml",
                fetched=2,
                candidates=1,
                filtered=0,
                ingested=1,
                skipped=0,
                failed=0,
            )
            store.record_run_source(
                "run-1", "openai", fetched=3, candidates=2, filtered=1, failed=1
            )
            store.record_run_end(
                "run-1",
                summary_json=json.dumps(
                    {
                        "fetched": 10,  # 与 run_sources 求和不同，验证 summary 优先
                        "candidates": 5,
                        "filtered": 2,
                        "expired": 4,
                        "ingested": 3,
                        "skipped": 1,
                        "failed": 1,
                        "pending": 1,
                        "sources_ok": 1,
                        "sources_failed": 1,
                        "elapsed_s": 42.5,
                        "exit_code": 0,
                    }
                ),
                exit_code=0,
                finished_at="2026-10-19T12:00:42Z",
            )
            # 一轮尚未结束的运行：没有 summary_json，统计回退到 run_sources
            store.record_run_start("run-2", "once", started_at="2026-10-20T08:00:00Z")
            store.record_run_source("run-2", "aws_ml", fetched=4, candidates=2, ingested=1)

        self._write(seed)
        overview = self._news().overview(limit=10)

        self.assertTrue(overview["available"])
        self.assertEqual(
            overview["counts"],
            {
                "new": 1,
                "classified": 0,
                "filtered_out": 1,
                "failed": 0,
                "ingest_pending": 0,
                "ingested": 1,
                "skipped": 0,
                "total": 3,
            },
        )

        sources = {item["source_id"]: item for item in overview["sources"]}
        self.assertEqual(sources["aws_ml"]["name"], "AWS Machine Learning Blog")
        self.assertTrue(sources["aws_ml"]["enabled"])
        self.assertEqual(sources["aws_ml"]["last_status"], 200)
        self.assertFalse(sources["openai"]["enabled"])
        self.assertEqual(sources["openai"]["consecutive_failures"], 1)
        self.assertEqual(sources["openai"]["last_error"], "upstream unavailable")
        # 注册表里没有的源也要展示，不能从页面上消失
        self.assertEqual(sources["legacy"]["name"], "legacy")

        self.assertEqual([run["run_id"] for run in overview["runs"]], ["run-2", "run-1"])
        running = overview["runs"][0]
        self.assertIsNone(running["exit_code"])
        self.assertEqual(running["fetched"], 4)
        self.assertEqual(running["ingested"], 1)
        finished = overview["runs"][1]
        self.assertEqual(finished["fetched"], 10)
        self.assertEqual(finished["expired"], 4)
        self.assertEqual(running["expired"], 0)  # 没有 summary_json 时按 0 兜底
        self.assertEqual(finished["elapsed_s"], 42.5)
        self.assertEqual(finished["exit_code"], 0)
        self.assertEqual(len(finished["sources"]), 2)
        self.assertEqual(finished["sources"][0]["source_name"], "AWS Machine Learning Blog")

    # ------------------------------------------------------------------
    def test_items_filters_pagination_and_like_escape(self) -> None:
        def seed(store: StateStore) -> None:
            self._article(
                store,
                url="https://example.com/1",
                source_id="aws_ml",
                title="Claude 100% local",
                fetched_at="2026-10-19T08:00:00Z",
            )
            store.set_status(
                "https://example.com/1",
                "ingested",
                title_zh="Claude 本地部署",
                summary_zh="一条关于 Claude 的新闻。",
            )
            self._article(
                store,
                url="https://example.com/2",
                source_id="openai",
                title="under_score release",
                fetched_at="2026-10-19T09:00:00Z",
            )
            self._article(
                store,
                url="https://example.com/3",
                source_id="openai",
                title="Plain announcement",
                fetched_at="2026-10-19T10:00:00Z",
            )
            store.set_status("https://example.com/3", "filtered_out", reason="无关")

        self._write(seed)
        news = self._news()

        by_status = news.items(status="ingested")
        self.assertEqual(by_status["total"], 1)
        self.assertEqual(by_status["items"][0]["title_zh"], "Claude 本地部署")

        by_source = news.items(source_id="openai")
        self.assertEqual(by_source["total"], 2)

        # LIKE 必须转义：_ 不能退化成「任意一个字符」
        underscore = news.items(q="under_score")
        self.assertEqual(underscore["total"], 1)
        self.assertEqual(underscore["items"][0]["title"], "under_score release")

        percent = news.items(q="100%")
        self.assertEqual(percent["total"], 1)
        self.assertEqual(percent["items"][0]["title"], "Claude 100% local")

        # 中文摘要也在搜索范围内
        self.assertEqual(news.items(q="Claude 的新闻")["total"], 1)

        page1 = news.items(page=1, page_size=2)
        page2 = news.items(page=2, page_size=2)
        self.assertEqual(page1["total"], 3)
        self.assertEqual(len(page1["items"]), 2)
        self.assertEqual(len(page2["items"]), 1)
        # 按 COALESCE(published_at, fetched_at) 倒序
        self.assertEqual(page1["items"][0]["title"], "Plain announcement")

        with self.assertRaises(ValueError):
            news.items(status="not-a-status")
        with self.assertRaises(ValueError):
            news.items(source_id="../etc")

    # ------------------------------------------------------------------
    def test_detail_key_facts_and_content_fallback(self) -> None:
        def seed(store: StateStore) -> None:
            self._article(
                store,
                url="https://example.com/doc",
                source_id="aws_ml",
                title="Doc article",
                fetched_at="2026-10-19T08:00:00Z",
            )
            store.set_status(
                "https://example.com/doc",
                "ingested",
                title_zh="文档文章",
                summary_zh="中文摘要",
                category="model_release",
                importance=5,
                key_facts_json=json.dumps(["事实一", "事实二"]),
                doc_text="这是清洗后的正文。\n第二段。",
                doc_filename="ai-news-aws_ml-doc.txt",
                ingest_result="success",
                job_id="job-1",
                retry_count=2,
            )
            self._write_feed_article(store)

        self._write(seed)
        news = self._news()

        doc = news.detail(1)
        assert doc is not None
        self.assertEqual(doc["key_facts"], ["事实一", "事实二"])
        self.assertEqual(doc["content_kind"], "document")
        self.assertIn("清洗后的正文", doc["content"])
        self.assertEqual(doc["doc_filename"], "ai-news-aws_ml-doc.txt")
        self.assertEqual(doc["job_id"], "job-1")
        self.assertEqual(doc["retry_count"], 2)
        self.assertEqual(doc["summary_zh"], "中文摘要")
        self.assertIsNone(doc["next_retry_at"])

        feed = news.detail(2)
        assert feed is not None
        self.assertEqual(feed["content_kind"], "feed")
        self.assertIn("Hello world", feed["content"])
        self.assertEqual(feed["key_facts"], [])

        empty = news.detail(3)
        assert empty is not None
        self.assertEqual(empty["content_kind"], "none")
        self.assertEqual(empty["content"], "")

        self.assertIsNone(news.detail(999))

    def _write_feed_article(self, store: StateStore) -> None:
        item_id = self._article(
            store,
            url="https://example.com/feed",
            source_id="openai",
            title="Feed article",
            fetched_at="2026-10-19T09:00:00Z",
        )
        store.set_status(
            "https://example.com/feed",
            "ingest_pending",
            feed_summary="<p>Hello <b>world</b></p>",
        )
        self._article(
            store,
            url="https://example.com/empty",
            source_id="openai",
            title="Empty article",
            fetched_at="2026-10-19T10:00:00Z",
        )
        store.set_status("https://example.com/empty", "new")

    # ------------------------------------------------------------------
    def test_missing_db_is_reported_not_created(self) -> None:
        missing = self.tmp / "not-there" / "news.db"
        news = NewsStore(db_path=missing, sources_file=self.sources_file)

        overview = news.overview()
        self.assertFalse(overview["available"])
        self.assertEqual(overview["counts"]["total"], 0)
        self.assertEqual(len(overview["sources"]), 2)
        self.assertEqual(overview["runs"], [])

        page = news.items(page=1, page_size=20)
        self.assertFalse(page["available"])
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)

        self.assertIsNone(news.detail(1))
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())


if __name__ == "__main__":
    unittest.main()
