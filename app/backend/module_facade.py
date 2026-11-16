"""拆包门面的模块属性兼容，保留原调用方的读取、替换和恢复行为。"""
import sys
from types import ModuleType


def install(name: str, implementation: ModuleType, domains: tuple[ModuleType, ...],
            owners: dict[str, ModuleType]) -> None:
    bindings = {
        key: tuple(module for module in domains if key in vars(module))
        for key in implementation.__all__
    }

    class Facade(ModuleType):
        def __getattribute__(self, key):
            owner = owners.get(key)
            if owner is not None:
                return getattr(owner, key)
            return super().__getattribute__(key)

        def __setattr__(self, key, value):
            super().__setattr__(key, value)
            for module in bindings.get(key, ()):
                setattr(module, key, value)

        def __delattr__(self, key):
            super().__delattr__(key)
            for module in bindings.get(key, ()):
                if key in vars(module):
                    delattr(module, key)

    implementation.__class__ = Facade
    sys.modules[name] = implementation
