"""执行器兼容门面，保留旧导入与模块属性替换语义。"""
import sys
from types import ModuleType

from runners_pkg import core as _implementation
from runners_pkg.core import *  # noqa: F403
from runners_pkg.core import __all__ as __all__


class _RunnerFacade(ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name not in self.__all__:
            return
        # 固定读取点映射使删除后的恢复也能回到所有子域。
        for module in _BINDINGS.get(name, ()):
            setattr(module, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for module in _BINDINGS.get(name, ()):
            if name in vars(module):
                delattr(module, name)


_DOMAIN_MODULES = (
    'runners_pkg.protocol',
    'runners_pkg.common',
    'runners_pkg.inputs',
    'runners_pkg.video',
    'runners_pkg.image',
    'runners_pkg.text',
    'runners_pkg.audio',
    'runners_pkg.cloud',
    'runners_pkg.results',
)
_BINDINGS = {
    name: tuple(sys.modules[module] for module in _DOMAIN_MODULES
                if name in vars(sys.modules[module]))
    for name in _implementation.__all__
}
_implementation.__class__ = _RunnerFacade
sys.modules[__name__] = _implementation
