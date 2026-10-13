import os

class UnsupportedFileTypeError(ValueError):
    """文件扩展名不在支持列表时抛出。"""

def extract_text(filename: str, content: bytes) -> str:
    #将多种格式解析为纯文本
    #拓展名
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".txt", ".md", ".csv"):
        return _read_text(content)

def _read_text(content: bytes) -> str:
    return content.decode("utf-8")
