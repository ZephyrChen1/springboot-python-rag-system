import re
import os
import json
from langchain_core.chat_history import BaseChatMessageHistory
from typing import Sequence
from langchain_core.messages import message_to_dict, messages_from_dict, BaseMessage

#session_id白名单
# session_id 会被直接拼进历史文件路径，外部输入必须过滤，否则可路径穿越
# （如 session_id="../../etc/passwd" 覆盖任意文件）。仅允许字母/数字/下划线/连字符。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

def get_history(session_id):
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"非法的 session_d : {session_id}"
            "（仅允许字母/数字/下划线/连字符，长度 1-128）"
        )
    return FileChatMessageHistory(session_id, storage_path = "./chat_history")

class FileChatMessageHistory(BaseChatMessageHistory):
    def __init__(self, session_id, storage_path):
        self.session_id = session_id
        self.storage_path = storage_path

        self.file_path = os.path.join(self.storage_path, self.session_id)
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)

    def add_messages(self, messages: Sequence[BaseMessage]):
        all_messages = list(self.messages) #先获取原有的消息列表,把原message消息放在一个列表里
        all_messages.extend(messages) #和新的消息融合成一个list

        #由于每个message都无法直接转换为json，故应该先转为字典或列表

        new_messages = [message_to_dict(message) for message in all_messages] #把每个message转化为字典，再把所有message弄成一个列表
        #将数据写入文件
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(new_messages, f)

    @property #将message方法变成成员属性用,好像不加会有问题
    def messages (self) -> list[BaseMessage]:
        #当前文件内：List[字典]
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                messages_data = json.load(f) #把json load 成 python对象
                return messages_from_dict(messages_data) #把dict转为messages
        except FileNotFoundError:
            return []

    def clear(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump([], f)



