import streamlit as st
from streamlit import file_uploader
from knowledge_base import KnowledgeBaseService
import threading
import time
from lightrag_service import LightRAGService
from document_loader import UnsupportedFileTypeError, extract_text
import config_data as config
from knowledge_base import read_ingest_progress, describe_ingest_stage


st.title('知识库更新')

uploader_file = st.file_uploader(
    "请上传文件",

)


# 入库后台线程（仅保证进程内唯一在跑的入库任务）
if "_ingest_lock" not in st.session_state:
    st.session_state["_ingest_lock"] = threading.Lock()
if "_ingest_thread" not in st.session_state:
    st.session_state["_ingest_thread"] = None

#状态维护
if "service" not in st.session_state:
    st.session_state["service"] = KnowledgeBaseService()


def _run_ingest(service: KnowledgeBaseService, text: str, file_name: str, result_holder: dict):
    """后台执行入库，结果写回 result_holder 字典（跨线程传递）。

    注意：service 必须在主线程取出后传入——st.session_state 绑定脚本运行线程，
    后台线程直接访问会 KeyError。
    """
    try:
        result_holder["result"] = service.upload_by_str(text, file_name)
    except Exception as exc:  # 入库异常也要带回主线程展示，而不是静默失败
        result_holder["result"] = f"[失败]入库出错：{exc}"


def _start_ingest(service: KnowledgeBaseService, text: str, file_name: str) -> dict:
    """启动后台入库线程，返回结果占位 dict：
    {"done": bool, "result": str, "file_name": str}。
    """
    holder = {"done": False, "result": "", "file_name": file_name}

    def _target():
        try:
            _run_ingest(service, text, file_name, holder)
        finally:
            holder["done"] = True

    thread = threading.Thread(target=_target, daemon=True)
    st.session_state["_ingest_thread"] = thread
    thread.start()
    return holder

def _show_progress(file_name: str, holder: dict):
    """轮询 doc_status.json 展示真实入库进度，直到后台线程完成。"""
    t0 = time.time()
    last_stage = None
    with st.status(f"正在载入知识库：{file_name}", expanded=True) as status:
        # 用占位容器反复重写进度行，避免 status 里堆积历史行
        ph = st.empty()
        while not holder["done"]:
            elapsed = time.time() - t0
            progress = read_ingest_progress(config.lightrag_working_dir, file_name)
            stage = describe_ingest_stage(progress, elapsed)
            if stage != last_stage:
                ph.markdown(stage)
                last_stage = stage
            time.sleep(0.3)

        # 入库已结束：若最后一条仍是"抽取中"（极小文档可能没来得及刷新），补一个完成提示
        progress = read_ingest_progress(config.lightrag_working_dir, file_name)
        if progress is not None and progress.get("status") == "processed":
            status.update(label="入库完成", state="complete", expanded=False)
        elif holder["result"].startswith("[跳过]"):
            status.update(label="内容已存在，跳过入库", state="complete", expanded=False)
        else:
            status.update(label="入库结束", state="complete", expanded=False)
        ph.empty()

    if holder["result"].startswith("[成功]"):
        st.success(holder["result"])
    elif holder["result"].startswith("[跳过]"):
        st.info(holder["result"])
    else:
        st.error(holder["result"])


if uploader_file is not None:
    file_name = uploader_file.name
    file_type = uploader_file.type
    file_size = uploader_file.size / 1024  # KB

    st.subheader(f"文件名：{file_name}")
    st.write(f"格式:{file_type} | 大小:{file_size:.1f}KB")

    content = uploader_file.getvalue()  # getvalue返回bytes，按格式解析

    # ==== [多格式解析] 先解析一次拿纯文本，异常在此拦截 ====
    try:
        text = extract_text(file_name, content)
    except UnsupportedFileTypeError as e:
        st.error(f"不支持的文件格式：{e}")
        st.stop()
    except ValueError as e:
        st.error(str(e))
        st.stop()

    st.caption(f"已解析 {len(text)} 字符，预览：")
    st.code(text[:200], language=None)

    # ==== [入库进度反馈] 后台入库 + 轮询进度 ====
    # Streamlit 每次交互都会重跑本脚本：入库必须在后台线程执行，
    # 主线程轮询 doc_status.json 展示进度；用全局锁防止重跑时重复启动入库。
    holder = st.session_state.get("_ingest_holder")
    current_thread = st.session_state.get("_ingest_thread")

    # 有入库在跑且就是当前文件：沿用已有 holder，避免重跑时重复提交
    if (
        holder is not None
        and not holder["done"]
        and current_thread is not None
        and current_thread.is_alive()
        and holder.get("file_name") == file_name
    ):
        _show_progress(file_name, holder)
    elif (
        current_thread is not None
        and current_thread.is_alive()
        and holder is not None
        and not holder.get("done")
        and holder.get("file_name") != file_name
    ):
        # 上一个文件仍在入库，拒绝并发启动第二个（LightRAG 单实例并发写有风险）
        st.warning(f"「{holder['file_name']}」仍在入库中，请稍候再上传新文件")
    else:
        with st.session_state["_ingest_lock"]:
            # 双重检查：锁内再确认没有在跑的同名入库
            current_thread = st.session_state.get("_ingest_thread")
            current_holder = st.session_state.get("_ingest_holder")
            if (
                current_thread is not None
                and current_thread.is_alive()
                and current_holder is not None
                and current_holder.get("file_name") == file_name
            ):
                holder = current_holder
            else:
                # service 在主线程取出再传线程（st.session_state 线程不共享）
                holder = _start_ingest(st.session_state["service"], text, file_name)
                st.session_state["_ingest_holder"] = holder
        _show_progress(file_name, holder)
