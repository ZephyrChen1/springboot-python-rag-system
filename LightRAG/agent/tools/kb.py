"""知识库相关工具。

kb_search   —— Phase 1，只读
kb_status   —— Phase 3，只读
kb_ingest   —— Phase 3，写操作，需要 AGENT_ALLOW_WRITE_TOOLS
kb_delete   —— Phase 3，写操作，需要 AGENT_ALLOW_WRITE_TOOLS + 人工确认

注意：没有 kb_stage 工具。暂存是 HTTP 接口（Java 上传文件用），
文件内容不能经过模型上下文——那既贵，又给了提示注入可乘之机。
"""

import asyncio
import json
import os

from lightrag import QueryParam

import config_data as config
from knowledge_base import describe_ingest_stage, forget_md5, read_ingest_progress
from rag_api.ingest import (
    IngestBusyError,
    find_job_by_file_name,
    get_job,
    submit_ingest,
    wait_job,
)
from rag_api.staging import (
    delete_staged,
    find_staged_by_name,
    load_staged,
    staged_exists,
)
from .base import Tool, ToolContext, ToolResult, register

# 删除工具的最长等待。删除本身可能被请求取消（断连），但线程里的
# LightRAG 调用会继续跑完——因此超时文案要说“可能仍在执行”，而不是“失败”。
_DELETE_TIMEOUT_S = 900.0


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class KbSearchTool(Tool):
    name = "kb_search"
    description = (
        "在企业知识库中检索与问题相关的内容。"
        "当问题涉及公司制度、产品文档、内部资料时使用。"
        "如果只是计算、查时间或常识问题，不要调用本工具。"
        "可以多次调用：换关键词，或换检索模式。"
        "不要为了凑步骤反复检索同一个问题。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索语句。尽量使用文档里可能出现的术语，而不是口语转述。",
            },
            "mode": {
                "type": "string",
                "enum": ["local", "global", "hybrid", "naive", "mix"],
                "description": (
                    "local 查具体实体的属性；global 查主题与总体关系；"
                    "hybrid 是两者结合，默认选它；naive 查原文细节；mix 是图+向量增强。"
                ),
            },
            "top_k": {
                "type": "integer",
                "minimum": 5,
                "maximum": 40,
                "default": 20,
                "description": "检索候选数。问题越宽泛可以越小，需要细节时调大。",
            },
        },
        "required": ["query"],
    }
    timeout_s = 60.0

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(False, "[参数错误] query 不能为空", error="bad_args")

        mode = args.get("mode") or config.lightrag_mode
        if mode not in ("local", "global", "hybrid", "naive", "mix", "bypass"):
            mode = config.lightrag_mode
        try:
            top_k = int(args.get("top_k") or config.lightrag_top_k)
        except (TypeError, ValueError):
            top_k = config.lightrag_top_k
        top_k = max(5, min(top_k, 40))

        param = QueryParam(mode=mode, top_k=top_k, enable_rerank=False)
        try:
            raw = await ctx.lightrag.aquery_data(query, param=param)
        except Exception as exc:
            return ToolResult(False, f"[检索失败] {exc}", error="query_failed")

        if not isinstance(raw, dict) or raw.get("status") != "success":
            message = raw.get("message") if isinstance(raw, dict) else "检索返回格式异常"
            return ToolResult(False, f"[检索失败] {message}", error="query_failed")

        data = raw.get("data") or {}
        entities = data.get("entities") or []
        relationships = data.get("relationships") or []
        chunks = data.get("chunks") or []
        metadata = raw.get("metadata") or {}
        used_mode = metadata.get("query_mode") or mode

        lines = [
            f"检索模式={used_mode}，命中 {len(entities)} 个实体、"
            f"{len(relationships)} 条关系、{len(chunks)} 个文本块。"
        ]

        # 只给模型少量高价值内容；完整数据放在 data 里给前端。
        for ent in entities[:5]:
            name = ent.get("entity_name") or "?"
            desc = _truncate(ent.get("description") or "", 160)
            lines.append(f"- 实体[{name}]（{ent.get('entity_type') or '未知类型'}）：{desc}")

        for chunk in chunks[:5]:
            file_path = chunk.get("file_path") or "?"
            content = _truncate((chunk.get("content") or "").strip(), 400)
            lines.append(f"- 原文[{file_path}]：{content}")

        if len(chunks) > 5:
            lines.append(f"（另有 {len(chunks) - 5} 个文本块未展示）")
        if not entities and not chunks:
            lines.append("知识库中没有检索到相关内容。不要编造，直接告诉用户未找到。")

        summary = _truncate("\n".join(lines), config.agent_tool_result_max_chars)

        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "mode": used_mode,
                "counts": {
                    "entities": len(entities),
                    "relationships": len(relationships),
                    "chunks": len(chunks),
                },
                "chunks": [
                    {
                        "file_path": c.get("file_path"),
                        "chunk_id": c.get("chunk_id"),
                        "content": _truncate((c.get("content") or "").strip(), 500),
                    }
                    for c in chunks[:10]
                ],
                "references": data.get("references") or [],
            },
        )


class KbStatusTool(Tool):
    name = "kb_status"
    description = (
        "查询文档入库进度。当用户问「入库好了吗」「处理到哪一步了」，"
        "或需要确认某份文档是否已经可以被检索时使用。"
        "参数二选一：job_id（入库任务 ID）或 file_name（文件名）。"
        "只给 file_name 时，会依次查最近入库任务、doc_status、暂存区。"
        "返回 [未找到] 表示这份文件既没入过库、也不在暂存区。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "入库任务 ID，提交入库时返回"},
            "file_name": {"type": "string", "description": "文档文件名"},
        },
        "required": [],
    }
    timeout_s = 10.0

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        job_id = (args.get("job_id") or "").strip()
        file_name = (args.get("file_name") or "").strip()

        if job_id:
            job = get_job(job_id)
            if job is None:
                return ToolResult(
                    False,
                    f"[未找到] job_id={job_id}（任务不存在或已过期）",
                    error="not_found",
                )
            return ToolResult(True, self._job_text(job), data=job)

        # 只给了文件名：任务注册表 → doc_status → 暂存区。
        # ★ 不能回退到「最新一条入库记录」：那会把别的文件的状态当成本文件的。
        if file_name:
            job = find_job_by_file_name(file_name)
            if job is not None:
                return ToolResult(True, self._job_text(job), data=job)

            progress = read_ingest_progress(config.lightrag_working_dir, file_name)
            if progress is not None:
                return ToolResult(
                    True,
                    self._progress_text(progress, file_name),
                    data={"progress": progress},
                )

            staged = find_staged_by_name(file_name)
            if staged is not None:
                size = staged.get("size")
                return ToolResult(
                    True,
                    f"「{file_name}」已上传到暂存区"
                    f"（{size if size is not None else '?'} 字节），尚未入库。"
                    f"可以调用 kb_ingest(file_name='{file_name}') 入库。",
                    data={"staged": staged},
                )

            return ToolResult(
                False,
                f"[未找到] 知识库和暂存区里都没有「{file_name}」。"
                "请让用户先点输入框左侧的「上传待入库文件」上传，而不是提供本机路径。",
                error="not_found",
            )

        # 什么都没指定：这是「最近一次入库是什么情况」，明确标注，避免被误读成
        # 用户说的那份文件。
        progress = read_ingest_progress(config.lightrag_working_dir)
        if progress is None:
            return ToolResult(True, "还没有任何入库记录。", data=None)
        return ToolResult(
            True,
            "最近一次入库记录（未指定文件）：\n"
            + self._progress_text(progress, None),
            data={"progress": progress},
        )

    @staticmethod
    def _job_text(job: dict) -> str:
        text = (
            f"任务 {job['job_id']}（{job['file_name']}）："
            f"{job.get('stage') or ('已完成' if job['done'] else '处理中')}，"
            f"已耗时 {job.get('elapsed_sec', 0)}s。"
        )
        if job.get("result"):
            text += f"\n结果：{_truncate(job['result'], 300)}"
        return text

    @staticmethod
    def _progress_text(progress: dict, file_name: str | None) -> str:
        text = describe_ingest_stage(progress)
        text += (
            f"\n文件：{progress.get('file_path') or file_name or '未知'}，"
            f"状态：{progress.get('status')}，"
            f"文本块：{progress.get('chunks_count', '?')}"
        )
        return text


class KbIngestTool(Tool):
    name = "kb_ingest"
    description = (
        "对已经上传并暂存的文档执行入库（切片、向量化、抽取实体关系）。"
        "只有在用户明确说「把这份文档入库」时才调用。"
        "参数二选一：staged_id（暂存接口返回），或 file_name（用户刚上传的文件名）。"
        "★ 你无法访问用户本机文件系统。用户只给文件名时，直接传 file_name 调用本工具；"
        "返回 [未找到] 说明文件还没上传，应提示用户点输入框左侧的「上传待入库文件」，"
        "不要索要本机路径、文件内容或让用户粘贴全文。"
        "入库耗时较长，调用前先告诉用户正在处理。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "staged_id": {
                "type": "string",
                "description": "上传接口 /v1/kb/stage 返回的暂存 ID",
            },
            "file_name": {
                "type": "string",
                "description": "暂存区中的文件名；不提供 staged_id 时使用最近一次同名暂存文件",
            },
            "timeout_s": {
                "type": "integer",
                "minimum": 60,
                "maximum": 1800,
                "default": 900,
                "description": "等待入库完成的最长秒数",
            },
        },
        "required": [],
    }
    timeout_s = 1800.0
    side_effect = True

    async def precheck(self, args: dict, ctx: ToolContext) -> ToolResult | None:
        """文件根本不在暂存区时，不要弹确认框让用户批准一个注定失败的操作。"""
        staged_id = (args.get("staged_id") or "").strip()
        file_name = (args.get("file_name") or "").strip()
        if staged_id:
            if not staged_exists(staged_id):
                return ToolResult(
                    False, f"[未找到] 暂存文件 {staged_id}", error="not_found"
                )
            return None
        if not file_name:
            return ToolResult(
                False, "[参数错误] 需要 staged_id 或 file_name", error="bad_args"
            )
        if find_staged_by_name(file_name) is None:
            return ToolResult(
                False,
                f"[未找到] 暂存区没有「{file_name}」。"
                "请让用户先点输入框左侧的「上传待入库文件」上传，再重试。",
                error="not_found",
            )
        return None

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        staged_id = (args.get("staged_id") or "").strip()
        file_name_arg = (args.get("file_name") or "").strip()

        if not staged_id:
            if not file_name_arg:
                return ToolResult(
                    False,
                    "[参数错误] 需要 staged_id 或 file_name",
                    error="bad_args",
                )
            staged = find_staged_by_name(file_name_arg)
            if staged is None:
                return ToolResult(
                    False,
                    f"[未找到] 暂存区没有「{file_name_arg}」。请让用户先上传文件。",
                    error="not_found",
                )
            staged_id = staged["staged_id"]

        try:
            file_name, content = load_staged(staged_id)
        except FileNotFoundError:
            return ToolResult(False, f"[未找到] 暂存文件 {staged_id}", error="not_found")
        except ValueError as exc:
            return ToolResult(False, f"[参数错误] {exc}", error="bad_args")

        try:
            job = submit_ingest(file_name, content)
        except IngestBusyError as busy:
            return ToolResult(
                False,
                f"[已有任务] 正在处理「{busy}」，请稍后再试或先查询 kb_status",
                error="busy",
            )

        # 内容已经交给入库线程；暂存文件没有保留价值。
        # 放在 submit 之后：提交失败（busy）时还能重试。
        try:
            delete_staged(staged_id)
        except OSError:
            pass

        timeout = int(args.get("timeout_s") or 900)
        try:
            final = await wait_job(job["job_id"], timeout=timeout)
        except TimeoutError:
            return ToolResult(
                False,
                f"[超时] 入库任务 {job['job_id']} 超过 {timeout}s 仍未完成，可用 kb_status 继续查询",
                error="timeout",
                data=job,
            )
        except KeyError:
            return ToolResult(False, f"[未找到] 任务 {job['job_id']}", error="not_found")

        result = final.get("result") or ""
        # [跳过] 表示内容已存在（MD5 去重），是成功的幂等结果，不是失败
        ok = not result.startswith("[失败]")
        return ToolResult(
            ok=ok,
            summary=f"入库任务 {final['job_id']} 已结束：{_truncate(result, 500)}",
            data={
                "job_id": final.get("job_id"),
                "file_name": final.get("file_name"),
                "done": final.get("done"),
                "result": _truncate(result, 1000),
                "elapsed_sec": final.get("elapsed_sec"),
            },
        )


def _find_doc_id(file_name: str) -> tuple[str | None, dict | None, int]:
    """在 doc_status 里按 file_path 找 doc_id，并返回同名命中数。

    LightRAG 的文件状态在 <working_dir>/kv_store_doc_status.json，
    结构为 {doc_id: {status, file_path, chunks_count, ...}}。

    同名文件可能有多份（不同目录/重复入库）。删除是破坏性操作，
    不能像检索那样“取第一个”，否则可能删错文档。
    """
    path = os.path.join(config.lightrag_working_dir, "kv_store_doc_status.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None, 0

    if not isinstance(data, dict):
        return None, None, 0

    target = os.path.basename(file_name.replace("\\", "/"))
    matches: list[tuple[str, dict]] = []
    for doc_id, row in data.items():
        if not isinstance(row, dict):
            continue
        row_path = os.path.basename(str(row.get("file_path") or "").replace("\\", "/"))
        if row_path == target:
            matches.append((doc_id, row))

    if not matches:
        return None, None, 0
    first_id, first_row = matches[0]
    return first_id, first_row, len(matches)


def _doc_content_hash(doc_id: str) -> str | None:
    """读取 full_docs 里该文档的 content_hash（= md5(原始内容)）。

    用于清理老格式的 md5.text（只有 hash、没有文件名的那批）。
    """
    path = os.path.join(config.lightrag_working_dir, "kv_store_full_docs.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    row = data.get(doc_id)
    if not isinstance(row, dict):
        return None
    value = row.get("content_hash")
    return value if isinstance(value, str) and value else None


class KbDeleteTool(Tool):
    name = "kb_delete"
    description = (
        "从知识库中删除一份文档及其派生数据（文本块、实体、关系）。"
        "这是不可逆操作，只在用户明确要求删除时才调用。"
        "调用前必须向用户确认文件名。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_name": {"type": "string", "description": "要删除的文档文件名"},
        },
        "required": ["file_name"],
    }
    timeout_s = _DELETE_TIMEOUT_S
    side_effect = True

    async def precheck(self, args: dict, ctx: ToolContext) -> ToolResult | None:
        """删除是破坏性操作：文件不存在/同名多份时，先给结论，别让用户白确认一次。"""
        file_name = (args.get("file_name") or "").strip()
        if not file_name:
            return ToolResult(False, "[参数错误] file_name 不能为空", error="bad_args")
        doc_id, _, matches = _find_doc_id(file_name)
        if not doc_id:
            return ToolResult(
                False, f"[未找到] 知识库中没有文件「{file_name}」", error="not_found"
            )
        if matches > 1:
            return ToolResult(
                False,
                f"[歧义] 有 {matches} 份同名文档「{file_name}」，"
                "请提供更精确的文件名（或先让用户在知识库页面确认）",
                error="ambiguous",
            )
        return None

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        file_name = (args.get("file_name") or "").strip()
        if not file_name:
            return ToolResult(False, "[参数错误] file_name 不能为空", error="bad_args")

        doc_id, row, matches = _find_doc_id(file_name)
        if not doc_id:
            return ToolResult(
                False,
                f"[未找到] 知识库中没有文件「{file_name}」",
                error="not_found",
            )
        if matches > 1:
            return ToolResult(
                False,
                f"[歧义] 有 {matches} 份同名文档「{file_name}」，"
                "请提供更精确的文件名（或先让用户在知识库页面确认）",
                error="ambiguous",
            )

        # 删除成功后 full_docs 里的记录也会消失，所以必须在删之前把
        # content_hash 取出来（用于清理旧格式的 md5.text）。
        content_hash = _doc_content_hash(doc_id)

        # 放到线程里执行同步删除：断连/取消只会停止等待，不会把删除本身
        # 拦腰截断（半截删除会留下孤立的实体/关系）。
        delete_sync = getattr(ctx.lightrag, "delete_by_doc_id", None)
        try:
            if callable(delete_sync):
                result = await asyncio.to_thread(delete_sync, doc_id)
            else:
                result = await ctx.lightrag.adelete_by_doc_id(doc_id)
        except Exception as exc:
            return ToolResult(False, f"[删除失败] {exc}", error="delete_failed")

        status = getattr(result, "status", "unknown")
        message = getattr(result, "message", str(result))
        ok = status == "success"

        removed = 0
        if ok:
            # 删除成功但仍留在 md5.text 里的记录会让下次上传永远被“跳过”。
            # 按文件名能精确清掉新格式；content_hash 兜底老格式。
            try:
                removed = forget_md5(
                    file_name=file_name,
                    content_hash=content_hash,
                )
            except Exception:
                pass

        summary = f"删除文档 {file_name}（{doc_id}）：{status}，{message}"
        if removed:
            summary += f"；已清理 {removed} 条去重索引"

        return ToolResult(
            ok=ok,
            summary=summary,
            data={
                "doc_id": doc_id,
                "file_name": file_name,
                "status": status,
                "message": message,
                "md5_entries_removed": removed,
            },
            error=None if ok else status,
        )


register(KbSearchTool())
register(KbStatusTool())
register(KbIngestTool())
register(KbDeleteTool())
