"""请求/响应的数据结构——**门面**，实体已经搬到 `schemas/` 包。

历史：这里原本是一个 2513 行的单文件，装着 172 个定义。2026-11-08 按域拆成
`schemas/` 下的 21 个模块（见 `schemas/__init__.py` 里的分层顺序与拆分说明），
本文件只保留 re-export，让 `from models import XxxIn` 这 25 个调用方一行不改。

**不要往这个文件里加新东西**：新模型加到 `schemas/<域>.py`，再在
`schemas/__init__.py` 里 re-export 一次。否则这里会重新长成第二个大文件。
"""
from schemas import *          # noqa: F401,F403  —— 门面：让 `from models import X` 照旧可用
from schemas import __all__ as _schemas_all

__all__ = list(_schemas_all)
