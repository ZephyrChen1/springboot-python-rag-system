"""开发用的假 agent 上游（监听 :8102）。

用途：前端 / Java BFF 联调时不需要真的把 LightRAG + 模型跑起来。
它按 Python 侧的真实协议输出 NDJSON，并实现一次可批准/拒绝的写操作确认。

    python -X utf8 tools/stub-agent-upstream.py

行为由消息内容决定：
  - 含「入库」→ thought → kb_ingest → confirm_required →（等确认）→ tool_result → answer → done
  - 其它      → thought → kb_search → tool_result → answer → done
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# run_id 必须每次请求都不一样：Java 的 chat_step 用 run_id + call_id 更新
# tool_result，固定 run_id 会让本次结果把历史行全部改掉（实测 Updates: 13）。
_state = threading.local()

# 只有一个待确认操作的槽位；开发联调同一时刻只会有一个人在用
_confirm_event = threading.Event()
_confirm_approved = {"value": True}


def _base(type_: str, **fields) -> dict:
    event = {
        "v": 1,
        "type": type_,
        "run_id": getattr(_state, "run_id", "stubrun0000"),
        "ts": int(time.time() * 1000),
    }
    event.update(fields)
    return event


def _chunk(wfile, payload: bytes) -> None:
    wfile.write(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")
    wfile.flush()


def _write(wfile, event: dict) -> None:
    _chunk(wfile, (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音；需要看流量时打开
        pass

    def _read_body(self) -> bytes:
        """Java 的 RestClient 对 Jackson 序列化的请求体可能用 chunked。

        只读 Content-Length 会把整个 JSON 读成空串 —— 表现为「前端明明传了
        approved=false，上游却收到 {}」，排查时很容易怀疑错层。
        """
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            parts = []
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                size = int(size_line, 16)
                if size == 0:
                    self.rfile.readline()  # 末尾 CRLF
                    break
                parts.append(self.rfile.read(size))
                self.rfile.read(2)  # 每块后面的 CRLF
            return b"".join(parts)

        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        try:
            return json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return {}

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path == "/ai/agent/confirm":
            body = self._read_json()
            print(f"confirm: {body}", flush=True)
            _confirm_approved["value"] = bool(body.get("approved", True))
            _confirm_event.set()
            self._json(200, {"ok": True, "approved": _confirm_approved["value"]})
            return

        if self.path != "/ai/agent/chat":
            self._json(404, {"detail": f"stub 不支持 {self.path}"})
            return

        body = self._read_json()
        message = str(body.get("message") or "")
        session_id = str(body.get("session_id") or "")
        user_id = int(self.headers.get("X-User-Id") or 0)
        _state.run_id = f"stubrun{int(time.time() * 1000)}"

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            _write(self.wfile, _base("start", session_id=session_id, user_id=user_id))
            time.sleep(0.15)

            if "入库" in message:
                _write(self.wfile, _base("thought", step=1,
                                         text="用户要写入知识库，先确认文件再调用 kb_ingest。"))
                time.sleep(0.1)
                args = {"file_name": "demo.txt"}
                _write(self.wfile, _base("tool_call", step=1, call_id="call-1",
                                         name="kb_ingest", args=args))
                time.sleep(0.1)

                _confirm_event.clear()
                _write(self.wfile, _base("confirm_required", step=1, call_id="call-1",
                                         name="kb_ingest", args=args,
                                         token="stub-token", expires_at=time.time() + 60))
                # 真实实现里 agent 循环在这里阻塞；stub 用 Event 模拟
                _confirm_event.wait(timeout=60)
                approved = _confirm_approved["value"]

                _write(self.wfile, _base(
                    "tool_result", step=1, call_id="call-1", ok=approved,
                    summary="已写入知识库" if approved else "[未确认] 用户未批准写操作",
                    **({} if approved else {"error": "not_confirmed"}),
                ))
                time.sleep(0.1)
                answer = "已按你的决定处理。" if approved else "好的，已取消这次写操作。"
            else:
                _write(self.wfile, _base("thought", step=1,
                                         text="这是一个知识库问题，先检索再回答。"))
                time.sleep(0.1)
                _write(self.wfile, _base("tool_call", step=1, call_id="call-1",
                                         name="kb_search", args={"query": message[:40]}))
                time.sleep(0.45)  # 让卡片上的耗时有意义
                _write(self.wfile, _base(
                    "tool_result", step=1, call_id="call-1", ok=True,
                    summary="命中 2 个文本块。\n[1] 计算机组成原理 ...\n[2] 操作系统 ...",
                ))
                time.sleep(0.1)
                answer = "根据知识库检索结果，这是 stub 生成的回答。"

            for piece in (answer[: len(answer) // 2], answer[len(answer) // 2:]):
                _write(self.wfile, _base("answer", delta=piece))
                time.sleep(0.15)

            _write(self.wfile, _base("done", steps=2, usage=None, finish_reason="stop"))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 前端点了「停止生成」/ 关了页面：Java 会断开这条连接
            pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8102), Handler)
    print("stub agent upstream -> http://127.0.0.1:8102  (Ctrl+C 退出)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
