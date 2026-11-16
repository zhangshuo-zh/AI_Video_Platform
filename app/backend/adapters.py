"""适配器向后兼容门面。实现维护于 adapters_pkg，保留属性替换的观测语义。"""
import sys
from adapters_pkg import core as _implementation
from adapters_pkg.core import *  # noqa: F403
from adapters_pkg.core import __all__ as __all__

sys.modules[__name__] = _implementation
