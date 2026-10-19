"""导入即注册。

新增工具时，在对应模块里用 `register(...)` 注册，并把模块加到这里。
"""

from . import base, biz, external, kb  # noqa: F401

# biz.py 内部会在「未配置 AGENT_BIZ_QUERY_KEY」时不注册 biz_query，
# 避免出现一个永远失败的、模型却看得见的工具。
