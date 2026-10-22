"""入库结果必须以 doc_status 终态为准。

回归背景：LightRAG 的 ainsert 在实体抽取失败时仍会返回 track_id。
如果只看返回值就写 md5 指纹，会把失败文档记成“已入库”，后续重传
永远命中 [跳过]，用户再也无法重试。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import config_data as config
import knowledge_base as kb


class FakeLightRAG:
    def __init__(self, status="processed", error_msg=None, write_status=True):
        self.status = status
        self.error_msg = error_msg
        self.write_status = write_status
        self.calls = []

    def insert_text(self, text, file_path=None):
        self.calls.append((text, file_path))
        if self.write_status:
            path = os.path.join(
                config.lightrag_working_dir, "kv_store_doc_status.json"
            )
            row = {
                "file_path": file_path,
                "status": self.status,
                "chunks_count": 1,
                "updated_at": "2026-10-21T00:00:00",
            }
            if self.error_msg:
                row["error_msg"] = self.error_msg
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"doc-1": row}, f, ensure_ascii=False)
        return "insert_test_track"


class UploadResultStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(config, "lightrag_working_dir", self.tmp.name),
            mock.patch.object(config, "md5_path", os.path.join(self.tmp.name, "md5.text")),
            mock.patch.object(kb, "_INGEST_STATUS_WAIT_S", 0.01),
        ]
        for p in self.patches:
            p.start()
        self.fake = FakeLightRAG()
        self.service_patch = mock.patch.object(
            kb, "get_lightrag_service", return_value=self.fake
        )
        self.service_patch.start()

    def tearDown(self):
        self.service_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _upload(self, text="测试内容", filename="doc.txt"):
        service = kb.KnowledgeBaseService()
        return service.upload_by_str(text, filename)

    def test_processed_saves_fingerprint(self):
        text = "成功文档"
        result = self._upload(text)
        self.assertTrue(result.startswith("[成功]"))
        self.assertTrue(kb.check_md5(kb.get_string_md5(text)))

    def test_failed_does_not_save_fingerprint_and_can_retry(self):
        self.fake.status = "failed"
        self.fake.error_msg = "Arrearage"
        text = "失败文档"
        result = self._upload(text)
        self.assertTrue(result.startswith("[失败]"))
        self.assertIn("Arrearage", result)
        self.assertFalse(kb.check_md5(kb.get_string_md5(text)))

        # 再传一次必须重新调用入库，而不是命中 [跳过]
        self._upload(text)
        self.assertEqual(len(self.fake.calls), 2)

    def test_unconfirmed_status_is_fail_closed(self):
        self.fake.status = "processing"
        text = "状态未确认"
        result = self._upload(text)
        self.assertTrue(result.startswith("[失败]"))
        self.assertIn("未确认", result)
        self.assertFalse(kb.check_md5(kb.get_string_md5(text)))

    def test_missing_status_is_fail_closed(self):
        self.fake.write_status = False
        text = "没有状态记录"
        result = self._upload(text)
        self.assertTrue(result.startswith("[失败]"))
        self.assertFalse(kb.check_md5(kb.get_string_md5(text)))


if __name__ == "__main__":
    unittest.main()
