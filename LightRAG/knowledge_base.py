from document_loader import extract_text
import os
import json
import config_data as config
from lightrag_service import get_lightrag_service
from hashlib import md5

# LightRAG doc_status 状态名（见 lightrag.base.DocStatus）
_INGEST_STATUS_PENDING = "pending"
_INGEST_STATUS_PARSING = "parsing"
_INGEST_STATUS_ANALYZING = "analyzing"
_INGEST_STATUS_PROCESSING = "processing"
_INGEST_STATUS_PROCESSED = "processed"
_INGEST_STATUS_FAILED = "failed"

def check_md5(md5_str: str):
    """检查md5字符串是否已经被处理过了
    False表示没处理过，True表示处理过
    """
    if not os.path.exists(config.md5_path):
        open(config.md5_path, "w", encoding="utf-8").close()
        return False
    else:
        for line in open(config.md5_path, "r", encoding="utf-8").readlines():
            line = line.strip()  # 去除空格和回车
            if line == md5_str:
                return True

        return False


def save_md5(md5_str: str):
    """将传入的md5字符串，记录到文件内保存"""
    with open(config.md5_path, "a", encoding="utf-8") as f:
        f.write(md5_str + "\n")


def get_string_md5(input_str: str, encoding='utf-8'):
    """得到传入字符串的md5"""
    str_bytes = input_str.encode(encoding=encoding)  # 将字符串转为bytes字节数组
    return md5(str_bytes).hexdigest()  # 先传入字节数组，然后得到其十六进制字符串

def read_ingest_progress(working_dir: str, file_path: str | None = None) -> dict | None:
    """读取 LightRAG 入库进度（doc_status.json 实时落盘）。

    入库是 LLM 抽取实体/关系的过程，耗时较长且没有 ingestion 侧进度回调，
    但 LightRAG 会在 kv_store_doc_status.json 里随处理实时更新文档状态：
      pending → parsing/analyzing → processing → processed / failed
    本函数读取该文件，返回匹配 file_path 的最新记录，供 UI 轮询展示真实进度。

    Args:
        working_dir: LightRAG 工作目录（config.lightrag_working_dir）
        file_path:   目标文件名（可选）。不传时返回最新更新的记录。
    Returns:
        dict（含 status / file_path / chunks_count / created_at / updated_at 等）
        或 None（文件不存在 / 解析失败 / 无记录）
    """
    path = os.path.join(working_dir, "kv_store_doc_status.json")
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    if not isinstance(data, dict) or not data:
        return None

    # 匹配 file_path；未命中或未指定时取 updated_at 最新的记录
    if file_path:
        for row in data.values():
            if isinstance(row, dict) and row.get("file_path") == file_path:
                return row
    latest = max(
        (row for row in data.values() if isinstance(row, dict)),
        key=lambda r: r.get("updated_at", ""),
        default=None,
    )
    return latest


def describe_ingest_stage(progress: dict | None, elapsed_sec: float | None = None) -> str:
    """把 doc_status 记录转成人类可读的进度文案（供 UI 展示）。

    Args:
        progress: read_ingest_progress() 的返回值，None 表示尚无记录
        elapsed_sec: 已耗时（秒），传入则追加在文案里
    Returns:
        进度描述字符串，如 "实体/关系抽取中（2 个文本块）…"
    """
    tail = f"（{int(elapsed_sec)}s）" if elapsed_sec is not None else ""

    if progress is None:
        return f"已提交，等待解析…{tail}"

    status = progress.get("status")
    if status == _INGEST_STATUS_PENDING:
        return f"排队等待处理…{tail}"
    if status in (_INGEST_STATUS_PARSING, _INGEST_STATUS_ANALYZING):
        return f"内容解析中…{tail}"
    if status == _INGEST_STATUS_PROCESSING:
        chunks = progress.get("chunks_count")
        return f"实体/关系抽取中（{chunks} 个文本块）…{tail}"
    if status == _INGEST_STATUS_PROCESSED:
        chunks = progress.get("chunks_count")
        return f"已完成，写入 {chunks} 个文本块"
    if status == _INGEST_STATUS_FAILED:
        return f"处理失败：{progress.get('error_msg', '未知错误')}"
    return f"处理中…{tail}"


class KnowledgeBaseService(object):
    def __init__(self):
        os.makedirs(config.lightrag_working_dir, exist_ok=True) #向量数据库路径
        self.lightrag = get_lightrag_service()

    def upload_file(self, filename: str, content: bytes) -> str:
        text = extract_text(filename, content)
        return self.upload_by_str(text, filename)

    def upload_by_str(self, data: str, filename: str) -> str:
        md5_hex = get_string_md5(data)
        if check_md5(md5_hex):
            return "[跳过]内容已存在于知识库中"

        track_id = self.lightrag.insert_text(data, file_path=filename)
        save_md5(md5_hex)
        return f"[成功]内容已经成功载入知识库（LightRAG track={track_id}）"


