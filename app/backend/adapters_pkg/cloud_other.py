"""适配器规格。修改规格时在对应能力模块维护。"""
from __future__ import annotations
from .types import Adapter


_CLOUD_OTHER = (
    Adapter(code="jimeng", label="即梦（图像）", capability="image", backend="cloud",
            key_env="FV_KEY_JIMENG", source="设计稿候选，未核验",
            note="参数与计价的官方口径待 W17 复核。"),
    Adapter(code="wanx", label="通义万相（图像）", capability="image", backend="cloud",
            key_env="FV_KEY_WANX", source="设计稿候选，未核验",
            note="参数与计价的官方口径待 W17 复核。"),
    Adapter(code="volc", label="火山方舟（配音）", capability="tts", backend="cloud",
            aliases=("volces", "ark", "volcengine"),
            key_env="FV_KEY_VOLC", source="设计稿候选，未核验",
            note="参数与计价的官方口径待 W17 复核。"),
    Adapter(code="minimax", label="MiniMax（配音）", capability="tts", backend="cloud",
            key_env="FV_KEY_MINIMAX", source="设计稿候选，未核验",
            note="参数与计价的官方口径待 W17 复核。"),
)
