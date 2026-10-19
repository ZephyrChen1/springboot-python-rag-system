"""
==== [RAG 服务化封装] 知识库接口 ====
"""

import config_data as config
from fastapi import APIRouter, File, HTTPException, UploadFile
from ..ingest import IngestBusyError, get_job, submit_ingest
from ..staging import save_staged

router = APIRouter(prefix = "/v1/kb", tags = ["知识库"])  #知识库子路由

@router.post("/upload")
def kb_upload(file: UploadFile = File(...)):
    filename = (file.filename or "unknown").replace("\\", "/").rsplit("/", 1)[-1]
    ext = "." + filename.rsplit(".", 1)[-1].lower() #拓展名

    if ext not in config.supported_file_extensions:
        raise HTTPException(
            status_code = 422,
            detail = f"不支持的文件格式: {ext}（支持 {config.supported_file_extensions}）"
        ) #先进行拓展名校验

    content = file.file.read()
    file.file.close()

    try:
        return submit_ingest(filename, content)
    except IngestBusyError as busy: #IngestBusyError用来表示已有入库任务
        raise HTTPException(
            status_code = 409,
            detail = f"已有入库任务正在处理：「{busy}」，请稍后再试",
        )

@router.post("/stage")
def kb_stage(file: UploadFile = File(...)):
    """只暂存、不入库。供 agent 的 kb_ingest(staged_id) 使用。

    文件内容不经过模型上下文：模型只拿得到 staged_id。
    """
    filename = (file.filename or "unknown").replace("\\", "/").rsplit("/", 1)[-1]
    ext = "." + filename.rsplit(".", 1)[-1].lower()

    if ext not in config.supported_file_extensions:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的文件格式: {ext}（支持 {config.supported_file_extensions}）",
        )

    # 先看 Starlette 已经量好的大小，避免为一个超大文件先把内存吃满。
    size = getattr(file, "size", None)
    limit = config.agent_stage_max_bytes
    if isinstance(size, int) and size > limit:
        file.file.close()
        raise HTTPException(
            status_code=413,
            detail=f"文件过大：{size} 字节，上限 {limit} 字节",
        )

    content = file.file.read(limit + 1)
    file.file.close()
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大：超过上限 {limit} 字节",
        )
    return save_staged(filename, content)


@router.get("/ingest/{job_id}")
def kb_ingest(job_id: str):
    """轮询入库进度"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code = 404, detail = "任务不存在或已过期")
    return job

