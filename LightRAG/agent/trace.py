"""Run trace：每个事件追加一行 JSONL。

只写不读，不参与推理。作用是在 agent 多步失败后回答：
「它走到了第几步、调了什么工具、拿到了什么、为什么停」。

用 JSONL 而不是整份 JSON，是为了每步立即落盘——进程崩溃也能留下现场。
"""

import json
import os

import config_data as config


class TraceWriter:
    def __init__(self, run_id: str, directory: str | None = None):
        self.run_id = run_id
        self.directory = directory or config.agent_trace_dir
        self.path = os.path.join(self.directory, f"{run_id}.jsonl")
        try:
            os.makedirs(self.directory, exist_ok=True)
        except OSError:
            self.path = None

    def write(self, event: dict) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError:
            # trace 是旁路，写失败不能影响主流程
            pass
