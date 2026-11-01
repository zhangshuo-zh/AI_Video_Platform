"""Adapter 能力清单（W06）。

这张表回答的是"平台**凭什么**认为某个 provider 能用哪些参数"。

规格 01-4.8.2 的那条硬要求：**能力不能靠模型名猜**。所以每个档位都带
`source`——它要么来自本机 CLI 的 `--help`/参数表，要么来自模型卡或设计稿
§7.1 的硬规格；没有核过的一律不填参数，只写"未核验"。界面拿到空参数时
只会显示"该 provider 参数未核验"，不会自己脑补宽高或步数。

三条边界，改这张表之前先读：

1. **这里不执行任何模型**。W06 只做能力清单，health_check 也只看"运行时
   目录在不在、密钥变量有没有值"，不加载权重、不发网络请求。
2. **本地运行时路径必须由环境变量给**（`FASTVIDEO_<CODE>_HOME`）。写死
   /Users/... 会让部署到 Studio 后健康检查永远显示"坏"；给不出来就诚实地
   回 unknown，而不是假装 ok。
3. **密钥只认变量名**。清单里出现的 key_env 是"建议用哪个变量"，值永远
   只从进程环境读，回给前端的最多是一个布尔值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Param:
    """一个允许下发的参数。范围/枚举写清楚，W07 生成命令时按这张表校验。"""

    name: str
    type: str                      # int / float / bool / str / image
    default: object | None = None
    minimum: float | None = None
    maximum: float | None = None
    multiple_of: int | None = None
    note: str = ""

    def as_dict(self) -> dict:
        out = {"name": self.name, "type": self.type, "default": self.default}
        if self.minimum is not None:
            out["min"] = self.minimum
        if self.maximum is not None:
            out["max"] = self.maximum
        if self.multiple_of is not None:
            out["multiple_of"] = self.multiple_of
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class ModelSpec:
    code: str
    label: str
    params: tuple[Param, ...] = ()
    note: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "label": self.label,
                "params": [p.as_dict() for p in self.params], "note": self.note}


@dataclass(frozen=True)
class Adapter:
    code: str
    label: str
    capability: str
    backend: str
    source: str
    models: tuple[ModelSpec, ...] = ()
    aliases: tuple[str, ...] = ()
    key_env: str = ""              # 云端建议的密钥变量名；本地留空
    home_env: str = ""             # 本地运行时目录的环境变量名
    params_source: str = ""        # 参数从哪读来的；空＝未核验
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "capability": self.capability,
            "backend": self.backend,
            "source": self.source,
            "models": [m.as_dict() for m in self.models],
            "aliases": list(self.aliases),
            "key_env": self.key_env,
            "home_env": self.home_env,
            "params_source": self.params_source,
            "params_known": bool(self.params_source),
            "note": self.note,
        }


# ---------- 视频：本地 ----------
# 硬规格来自设计稿 §7.1 的视频档位表（FastVideo HEAD 6ded84ee 的参数表）。
# 1.3B 入口只有 --fast；5B 入口才有 --fast-spatial / --fast / --refine 三件套，
# 且 --fast-spatial 与 --refine 互斥——互斥关系写进 note，W07 校验时照读。
_FASTMETAL = Adapter(
    code="fastmetal",
    label="FastMetal（FastVideo MLX 栈）",
    capability="video",
    backend="local",
    source="设计稿 §7.1 视频档位表（FastVideo HEAD 6ded84ee）",
    aliases=("fast-video", "fastvideo"),
    home_env="FASTVIDEO_FASTMETAL_HOME",
    params_source="FastVideo 1.3B/5B 入口的脚本参数表",
    note="只有 T2V；I2V/TI2V 不在这个运行时里。",
    models=(
        ModelSpec(
            code="fastmetal-1.3b-qad",
            label="1.3B QAD（迭代）",
            note="入口 mlx_wan_prompt_to_video.py 只有 --fast 一个加速开关。",
            params=(
                Param("width", "int", 448, 64, 1920, 16),
                Param("height", "int", 832, 64, 1920, 16),
                Param("frames", "int", 77, 1, 257),
                Param("steps", "int", 3, 1, 50),
                Param("seed", "int", None, 0, 2147483647),
                Param("fast", "bool", False),
            ),
        ),
        ModelSpec(
            code="fastmetal-5b-qad",
            label="5B QAD（批量成片）",
            note="--fast-spatial 与 --refine 互斥；--fast 可与之组合。",
            params=(
                Param("width", "int", 704, 64, 1920, 16),
                Param("height", "int", 1280, 64, 1920, 16),
                Param("frames", "int", 121, 1, 257),
                Param("steps", "int", 3, 1, 50),
                Param("seed", "int", None, 0, 2147483647),
                Param("fast_spatial", "bool", False),
                Param("fast", "bool", False),
                Param("refine", "bool", False),
            ),
        ),
    ),
)

_MLX_VIDEO = Adapter(
    code="mlx-video",
    label="mlx-video（Blaizzy）",
    capability="video",
    backend="local",
    source="设计稿 §7.1（mlx-video 支持矩阵）",
    aliases=("mlx_video", "mlxvideo"),
    home_env="FASTVIDEO_MLX_VIDEO_HOME",
    params_source="mlx-video CLI 参数表（--image / --steps）",
    note="I2V 走 --image；默认 40 步在本机不现实，目录里只收 Lightning 4 步。",
    models=(
        ModelSpec(
            code="wan2.2-i2v-a14b",
            label="Wan2.2 I2V-A14B（参考图驱动）",
            params=(
                Param("image", "image", None, note="必填：条件图路径由 Job 的输入产物给"),
                Param("width", "int", 1280, 64, 1920, 16),
                Param("height", "int", 704, 64, 1920, 16),
                Param("frames", "int", 81, 1, 257),
                Param("steps", "int", 4, 1, 50, note="配 Wan2.2-Lightning LoRA 时取 4"),
                Param("seed", "int", None, 0, 2147483647),
            ),
        ),
        ModelSpec(
            code="wan2.2-t2v-a14b",
            label="Wan2.2 T2V-A14B（关键镜头）",
            params=(
                Param("width", "int", 1280, 64, 1920, 16),
                Param("height", "int", 704, 64, 1920, 16),
                Param("frames", "int", 81, 1, 257),
                Param("steps", "int", 4, 1, 50),
                Param("seed", "int", None, 0, 2147483647),
            ),
        ),
    ),
)

# ---------- 图像：本地 ----------
# 只收 FLUX.1-schnell：Apache-2.0，许可干净；dev 档的商用限制还没裁，先不进目录。
_MFLUX = Adapter(
    code="mflux",
    label="mflux（FLUX.1）",
    capability="image",
    backend="local",
    source="设计稿 §7.1「其他任务」表",
    home_env="FASTVIDEO_MFLUX_HOME",
    params_source="mflux CLI 参数表",
    note="图像走 mflux，不走 PyTorch MPS（MPS 出视频有静默坏帧）。",
    models=(
        ModelSpec(
            code="flux1-schnell",
            label="FLUX.1 schnell（Apache-2.0）",
            params=(
                Param("width", "int", 1024, 256, 1536, 16),
                Param("height", "int", 1024, 256, 1536, 16),
                Param("steps", "int", 4, 1, 12, note="schnell 是 4 步蒸馏档"),
                Param("seed", "int", None, 0, 2147483647),
            ),
        ),
    ),
)

# ---------- 配音 / 音乐：本地 ----------
_VOXCPM = Adapter(
    code="voxcpm2",
    label="VoxCPM2",
    capability="tts",
    backend="local",
    source="设计稿 §7.1「其他任务」表；口型走 Rhubarb viseme 分层",
    aliases=("voxcpm",),
    home_env="FASTVIDEO_VOXCPM_HOME",
    params_source="VoxCPM2 推理脚本参数（字级时间戳、拼音覆音）",
    note="返回字级时间戳；口型不是独立能力，挂在 tts 后处理。",
    models=(
        ModelSpec(
            code="voxcpm2",
            label="VoxCPM2",
            params=(
                Param("speed", "float", 1.0, 0.5, 2.0, note="语速倍率"),
                Param("seed", "int", None, 0, 2147483647),
            ),
        ),
    ),
)

_ACE_STEP = Adapter(
    code="ace-step",
    label="ACE-Step 1.5",
    capability="music",
    backend="local",
    source="设计稿 §7.1「其他任务」表",
    aliases=("acestep", "ace_step"),
    home_env="FASTVIDEO_ACE_STEP_HOME",
    params_source="ACE-Step CLI 参数表",
    note="MIT + MLX；失败保留状态，不自动换模型。",
    models=(
        ModelSpec(
            code="ace-step-1.5",
            label="ACE-Step 1.5",
            params=(
                Param("duration", "int", 60, 10, 300, note="秒"),
                Param("steps", "int", 27, 1, 100),
                Param("seed", "int", None, 0, 2147483647),
                Param("instrumental", "bool", True),
            ),
        ),
    ),
)

# ---------- 文本 / 云端：只登记，不下发参数 ----------
# 文本是本机唯一没有可用路线的能力（规格 02 §8.2 原文："没有合格的本地文本路线"）。
# 云端参数要到 W07 按官方 schema 核过才填，本轮 model 与 params 都留空让用户自己写 ID。
_TEXT_CLOUD = (
    Adapter(code="deepseek", label="DeepSeek（国内 API）", capability="text",
            backend="cloud", key_env="DEEPSEEK_API_KEY",
            source="现场环境里已有的密钥名（DEEPSEEK_API_KEY）",
            note="key_env 建议直接用 DEEPSEEK_API_KEY，避免再起一个 FV_KEY_ 名字。"),
    Adapter(code="volc", label="火山方舟", capability="text", backend="cloud",
            aliases=("volces", "ark", "volcengine"),
            key_env="FV_KEY_VOLC", source="设计稿 §7.3（建议供应商）",
            note="一个端点挂多家模型；模型 ID 与参数到 W07 核官方文档再填。"),
    Adapter(code="minimax", label="MiniMax", capability="text", backend="cloud",
            key_env="FV_KEY_MINIMAX", source="设计稿 §7.3（候选供应商）",
            note="模型 ID 与参数到 W07 核官方文档再填。"),
)

# 云端视频只登记 provider 名字：价格、模型档、参数都还没核，任何一项写死都会
# 变成"界面说有这个能力"的假承诺。开启前必须先过 W17 的许可与计价复核。
_VIDEO_CLOUD = tuple(
    Adapter(code=code, label=label, capability="video", backend="cloud",
            key_env="FV_KEY_" + code.upper().replace("-", "_"),
            source="设计稿 §7.3 / §7.5 候选，未核验",
            note="云端视频必须逐镜头显式选择；模型档/计价/参数待核验。")
    for code, label in (("wan", "Wan 2.5+ API"), ("kling", "可灵"), ("seedance", "Seedance"))
)

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

ADAPTERS: tuple[Adapter, ...] = (
    _FASTMETAL, _MLX_VIDEO, _MFLUX, _VOXCPM, _ACE_STEP,
    *_TEXT_CLOUD, *_VIDEO_CLOUD, *_CLOUD_OTHER,
)

# 同一 capability+backend 下 provider 允许用别名找（用户可能填 mlx_video）。
_INDEX: dict[tuple[str, str, str], Adapter] = {}
for _a in ADAPTERS:
    for _name in (_a.code, *_a.aliases):
        _INDEX[(_a.capability, _a.backend, _name.strip().lower())] = _a


def find(capability: str, backend: str, provider: str) -> Adapter | None:
    """按"能力 + 后端 + provider 标识"找登记项；找不到就是没登记，不猜。"""
    key = (capability, backend, (provider or "").strip().lower())
    return _INDEX.get(key)


def effective_key_env(provider: str, configured: str = "",
                      capability: str = "", backend: str = "cloud") -> str:
    """这条槽位到底去哪个环境变量找密钥。

    优先级：用户填的名字 > 清单给的建议名 > FV_KEY_<PROVIDER>。
    中间那一档是为了 DEEPSEEK_API_KEY 这种既有名字：选 deepseek 就自动用它，
    不必逼人再去 ~/.zshrc 里多加一份同值不同名的变量。
    """
    name = (configured or "").strip()
    if name:
        return name
    adapter = find(capability, backend, provider)
    if adapter is not None and adapter.key_env:
        return adapter.key_env
    provider = (provider or "").strip()
    if not provider:
        return ""
    return "FV_KEY_" + provider.upper().replace("-", "_").replace(" ", "_")


def key_present(env_name: str) -> bool:
    """只看有没有，不看值；空名字自然算没有。"""
    return bool(env_name) and bool(os.environ.get(env_name))


def health(row) -> dict:
    """实时健康：不加载权重、不发请求。

    四种状态分开，是因为人要做的事不同：
    unconfigured＝先选 provider；unregistered＝清单没这个 provider（不要硬填）；
    missing＝该配的东西没配；unknown＝平台检查不了，需要人去确认。
    """
    provider = (row["provider"] or "").strip()
    capability = row["capability"]
    backend = row["backend"]
    if not provider:
        return {"state": "unconfigured", "detail": "还没选 provider",
                "adapter": None, "key_env": ""}

    adapter = find(capability, backend, provider)
    if adapter is None:
        return {"state": "unregistered",
                "detail": "这个 provider 没有登记适配器，平台不会猜它的参数与运行方式",
                "adapter": None, "key_env": ""}

    base = {"adapter": {"code": adapter.code, "label": adapter.label,
                        "source": adapter.source, "params_source": adapter.params_source},
            "key_env": ""}
    if backend == "cloud":
        env = effective_key_env(provider, row["key_env"], capability, backend)
        base["key_env"] = env
        if not env:
            return {**base, "state": "unconfigured", "detail": "没有可用的密钥变量名"}
        if key_present(env):
            return {**base, "state": "ok", "detail": f"密钥来自环境变量 {env}"}
        return {**base, "state": "missing",
                "detail": f"环境变量 {env} 没有值；改完要重启服务"}

    home_env = adapter.home_env
    base["key_env"] = ""
    home = (os.environ.get(home_env) or "").strip() if home_env else ""
    if not home:
        return {**base, "state": "unknown",
                "detail": f"设 {home_env} 指向运行时目录后才会检查"}
    if Path(home).exists():
        return {**base, "state": "ok",
                "detail": "运行时目录在；权重是否就绪要到第一条作业才验"}
    return {**base, "state": "missing", "detail": f"{home_env} 指向的目录不存在"}


def manifest_payload() -> dict:
    """给界面和 W07 的唯一清单出口。"""
    return {
        "policy": {
            "local_default_capabilities": ["image", "video", "tts", "music"],
            "video_cloud_must_be_explicit": True,
            "no_auto_fallback": True,
            "note": "云端视频只能逐镜头显式选择；本地失败不会自动转云端。",
        },
        "adapters": [a.as_dict() for a in ADAPTERS],
    }
