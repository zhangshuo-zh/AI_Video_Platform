"""产线编排——**门面**，实体已经搬到 `pipeline_pkg/` 包。

历史：这里原本是 1860 行、71 个顶层定义的单个文件。2026-11-08 按域拆成
`pipeline_pkg/` 下的 8 个模块（见 `pipeline_pkg/__init__.py` 的模块划分与分层），
本文件只保留 re-export，让 `import pipeline; pipeline.xxx()` 这 6 个调用方一行不改。

**不要往这个文件里加新东西**：新函数加到 `pipeline_pkg/<域>.py`，再在
`pipeline_pkg/__init__.py` 里 re-export 一次。
"""
from pipeline_pkg import *          # noqa: F401,F403  —— 门面
from pipeline_pkg import __all__ as _pipeline_all

__all__ = list(_pipeline_all)
