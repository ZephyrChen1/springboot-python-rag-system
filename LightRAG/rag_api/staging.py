"""上传暂存区。

文件内容绝不经过模型上下文。流程：
  1. Java/前端把文件 POST 到 /v1/kb/stage，落盘并返回 staged_id；
  2. agent 决定入库时，kb_ingest(staged_id) 读取暂存文件并提交入库。

目录与 Lightrag_db 分开：暂存文件是可以随时删除的原始输入，
向量库和工作目录不是。
"""

import json
import os
import re
import time
import uuid

import config_data as config

_STAGED_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\-一-鿿]")
_MAX_NAME_LEN = 200
# 用户只上传不问入库时，暂存文件不会有人来删；给一个兜底 TTL。
_STAGE_TTL_S = 24 * 3600


def _safe_name(filename: str) -> str:
    name = os.path.basename((filename or "").replace("\\", "/"))
    name = _UNSAFE_CHARS.sub("_", name).strip("._")
    if not name:
        name = "upload.bin"
    return name[:_MAX_NAME_LEN]


def _paths(staged_id: str) -> tuple[str, str]:
    if not isinstance(staged_id, str) or not _STAGED_ID_RE.match(staged_id):
        raise ValueError("非法的 staged_id")
    return (
        os.path.join(config.agent_stage_dir, f"{staged_id}.bin"),
        os.path.join(config.agent_stage_dir, f"{staged_id}.json"),
    )


def _cleanup_old() -> None:
    """删除超过 TTL 的暂存文件。调用方已保证目录存在。

    只在 save_staged 时顺手做一次（低成本、无需后台线程）；
    单次失败不影响本次上传。
    """
    now = time.time()
    try:
        entries = os.listdir(config.agent_stage_dir)
    except OSError:
        return
    for entry in entries:
        if not (entry.endswith(".json") or entry.endswith(".bin")):
            continue
        path = os.path.join(config.agent_stage_dir, entry)
        try:
            if now - os.path.getmtime(path) > _STAGE_TTL_S:
                os.remove(path)
        except OSError:
            pass


def save_staged(filename: str, content: bytes) -> dict:
    os.makedirs(config.agent_stage_dir, exist_ok=True)
    _cleanup_old()
    staged_id = uuid.uuid4().hex
    data_path, meta_path = _paths(staged_id)
    safe_name = _safe_name(filename)

    with open(data_path, "wb") as f:
        f.write(content)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "staged_id": staged_id,
                "file_name": safe_name,
                "size": len(content),
            },
            f,
            ensure_ascii=False,
        )

    return {"staged_id": staged_id, "file_name": safe_name, "size": len(content)}


def find_staged_by_name(filename: str) -> dict | None:
    """按文件名找最近一次暂存记录。

    模型经常只说文件名（用户刚上传完），而不是 32 位 hex 的 staged_id。
    返回 {staged_id, file_name, size}；没有可用记录时返回 None。
    """
    target = _safe_name(filename)
    try:
        entries = os.listdir(config.agent_stage_dir)
    except OSError:
        return None

    best: tuple[float, dict] | None = None
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        meta_path = os.path.join(config.agent_stage_dir, entry)
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                continue
            staged_id = meta.get("staged_id")
            if not isinstance(staged_id, str) or not _STAGED_ID_RE.match(staged_id):
                continue
            if _safe_name(meta.get("file_name") or "") != target:
                continue
            data_path, _ = _paths(staged_id)
            if not os.path.exists(data_path):
                continue
            mtime = os.path.getmtime(meta_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue

        if best is None or mtime > best[0]:
            best = (
                mtime,
                {
                    "staged_id": staged_id,
                    "file_name": target,
                    "size": meta.get("size"),
                },
            )
    return best[1] if best else None


def staged_exists(staged_id: str) -> bool:
    """暂存数据文件是否还在（不读取内容，避免为一次校验把 100MB 读进内存）。"""
    try:
        data_path, meta_path = _paths(staged_id)
    except ValueError:
        return False
    return os.path.exists(data_path) and os.path.exists(meta_path)


def load_staged(staged_id: str) -> tuple[str, bytes]:
    """返回 (file_name, content)。文件不存在抛 FileNotFoundError。"""
    data_path, meta_path = _paths(staged_id)
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    file_name = _safe_name(meta.get("file_name") or "upload.bin")
    with open(data_path, "rb") as f:
        content = f.read()
    return file_name, content


def delete_staged(staged_id: str) -> None:
    data_path, meta_path = _paths(staged_id)
    for path in (data_path, meta_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
