"""跨模块复用的业务动作——**门面**，实体已经搬到 `services/` 包。

历史：这里原本是 2392 行、93 个顶层定义的单个文件。2026-11-08 按域拆成
`services/` 下的 12 个模块（见 `services/__init__.py` 里的模块划分与分层），
本文件只保留 re-export，让 `import services; services.xxx()` 这 13 个调用方一行不改。

**不要往这个文件里加新东西**：新函数加到 `services/<域>.py`，再在
`services/__init__.py` 里 re-export 一次。否则这里会重新长成第二个 2392 行。
"""
from services import *          # noqa: F401,F403  —— 门面
from services import __all__ as _services_all

__all__ = list(_services_all)
