"""适配器兼容入口。新增规格放到能力模块，行为放到 core。"""
from .core import *  # noqa: F403
from .core import __all__ as __all__
