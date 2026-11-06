"""Adapter 能力清单（W06 建立，W09 接执行）。

这张表回答的是"平台**凭什么**认为某个 provider 能用哪些参数"。

规格 01-4.8.2 的那条硬要求：**能力不能靠模型名猜**。所以每个档位都带
`source`——它要么来自本机 CLI 的 `--help`/参数表，要么来自模型卡或设计稿
§7.1 的硬规格；没有核过的一律不填参数，只写"未核验"。界面拿到空参数时
只会显示"该 provider 参数未核验"，不会自己脑补宽高或步数。

三条边界，改这张表之前先读：

1. **这里不执行任何模型**。清单与健康只回答"凭什么认为它能用、缺什么"。
   W09 起健康检查从"运行时目录在不在"升级为 `runtime.py` 的逐项探测
   （解释器、入口脚本、权重、CLI），仍然不加载权重、不发网络请求；
   真起子进程的是 `worker.py` + `runners.py`。
2. **本地运行时路径必须由环境变量给**（`FASTVIDEO_<CODE>_HOME`）。写死
   /Users/... 会让部署到 Studio 后健康检查永远显示"坏"；给不出来就诚实地
   回 unknown，而不是假装 ok。
3. **密钥只认变量名**。清单里出现的 key_env 是"建议用哪个变量"，值永远
   只从进程环境读，回给前端的最多是一个布尔值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
import engines
import runtime


@dataclass(frozen=True)
class Param:
    """一个允许下发的参数。范围/枚举写清楚，W07 生成命令时按这张表校验。"""

    name: str
    type: str                      # int / float / bool / str / image
    default: object | None = None
    minimum: float | None = None
    maximum: float | None = None
    multiple_of: int | None = None
    choices: tuple[str, ...] = ()  # 字符串枚举；空＝自由文本
    note: str = ""

    def as_dict(self) -> dict:
        out = {"name": self.name, "type": self.type, "default": self.default}
        if self.minimum is not None:
            out["min"] = self.minimum
        if self.maximum is not None:
            out["max"] = self.maximum
        if self.multiple_of is not None:
            out["multiple_of"] = self.multiple_of
        if self.choices:
            out["choices"] = list(self.choices)
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class Price:
    """一条官方计价口径。只写核过的数字，不写"大概"。

    金额同样是整数 micros，且**按计价单位**存：`unit=second` 时是每秒多少
    micros，`unit=million_token` 时是每百万 token 多少 micros。界面拿它预填
    "数量 × 单价"，服务端仍然按提交人确认的那一张单子记账。
    """

    label: str
    unit: str
    currency: str
    amount_micros: int
    source: str
    default_quantity: float = 1.0
    when: tuple[tuple[str, object], ...] = ()   # 与 params 的键值同时成立才适用
    note: str = ""

    def as_dict(self) -> dict:
        out = {
            "label": self.label,
            "unit": self.unit,
            "currency": self.currency,
            "amount_micros": self.amount_micros,
            "source": self.source,
            "default_quantity": self.default_quantity,
            "note": self.note,
        }
        if self.when:
            out["when"] = {k: v for k, v in self.when}
        return out


@dataclass(frozen=True)
class ModelSpec:
    code: str
    label: str
    params: tuple[Param, ...] = ()
    prices: tuple[Price, ...] = ()
    note: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "label": self.label,
                "params": [p.as_dict() for p in self.params],
                "prices": [p.as_dict() for p in self.prices],
                # 这一档能吃什么类型的**输入**，只从已登记的参数表里数出来
                # （type=="image" 就是它确实收一张图），不按模型名字猜：
                # "Wan2.2 I2V" 这个名字不足以说明 mflux 那一档也能收参考图——它不能。
                # 资产中心的图像面板据此老实说"这一档不接收参考图，一致性靠提示词文字"。
                "image_inputs": [p.name for p in self.params if p.type == "image"],
                "note": self.note}


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
    note="只有 T2V；I2V/TI2V 不在这个运行时里。DMD 固定 3 步，清单不摆假的步数旋钮。",
    models=(
        ModelSpec(
            code="fastmetal-1.3b-qad",
            label="1.3B QAD（迭代）",
            note="入口 mlx_wan_prompt_to_video.py；默认 640×352、81 帧、3 步 DMD。"
                 "16GB M4 Air 上 832×480/81 帧与 640×352/81 帧都会在 TAEHV 解码段 OOM"
                 "（W09 实测），所以默认开 --fast-spatial：按 320×176 去噪+解码再上采样。",
            params=(
                Param("width", "int", 640, 64, 1920, 16,
                      note="16GB 机器建议 640；832 会在解码段 OOM"),
                Param("height", "int", 352, 64, 1920, 16,
                      note="与 width 一起决定显存；352×640 已实测"),
                Param("frames", "int", 81, 1, 257,
                      note="81 帧 @16fps ≈ 5 秒；必须是 4n+1"),
                Param("seed", "int", 1024, 0, 2147483647),
                Param("fast", "bool", False),
                Param("fast_spatial", "bool", True,
                      note="默认开：半分辨率去噪+解码后上采样；16GB 机器关掉会在 81 帧 OOM"),
                # 下面三项的出处同样是本机那份入口脚本的 argparse 原文
                # （`examples/inference/basic/mlx_wan_prompt_to_video.py`）：
                # `--fps` 默认 16、`--fast-factor` 默认 2、`--fast-sharpen` 默认 0.6。
                Param("fps", "int", 16, 1, 60,
                      note="产物帧率。1.3B 出的默认是 16，成片侧默认按 24 出——"
                           "这一档差值现在由导出前的 RIFE 补帧补，不再靠复制帧"),
                Param("fast_factor", "int", 2, 1, 4,
                      note="开 --fast 时少生成几倍帧（2＝生成一半再 RIFE 补满）"),
                Param("fast_sharpen", "float", 0.6, 0, 2,
                      note="补帧后的轻度 unsharp，抵消插帧的糊；0＝关"),
            ),
        ),
        ModelSpec(
            code="fastmetal-5b-qad",
            label="5B QAD（批量成片）",
            note="入口 mlx_wan22_generate.py；默认 832×448、121 帧。"
                 "--fast-spatial 与 --refine 互斥；--fast 可与之组合。",
            params=(
                Param("width", "int", 832, 64, 1920, 16),
                Param("height", "int", 448, 64, 1920, 16),
                Param("frames", "int", 121, 1, 257),
                Param("seed", "int", 1234, 0, 2147483647),
                Param("fast_spatial", "bool", False),
                Param("fast", "bool", False),
                Param("refine", "bool", False),
                # 出处：本机 `examples/inference/basic/mlx_wan22_generate.py`
                # 的 argparse（--fps 默认 24、--fast-factor 默认 2、--fast-sharpen 默认 0.6）
                Param("fps", "int", 24, 1, 60),
                Param("fast_factor", "int", 2, 1, 4),
                Param("fast_sharpen", "float", 0.6, 0, 2),
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

# ---------- 图像：本机常驻服务（Draw Things）----------
# 它与 mflux 的分别**不是"另一个模型"，而是另一种执行形状**：这是应用里的常驻
# HTTP 服务，端点住在 `engine_instance`（0040），平台既起不了它，也没法编程换模型
# （2026-11-06 目标机实测：`POST /sdapi/v1/options` 回 404、`sd_model_checkpoint`
# 被 422 拒）。所以这一档的"当前用的是哪个权重"只能现读，见 G1b 已裁＝甲。
_DRAW_THINGS = Adapter(
    code="draw_things",
    label="Draw Things（本机应用）",
    capability="image",
    backend="local",
    source="本机 bundle 26.0924.0 的端点字符串＋社区仓 drawthingsai/draw-things-community"
           "（HEAD 7d5f686b）的 HTTPAPIServer/ServerModels/Parameters 三份一手文件；"
           "端点与返回体形状另见《文档/开发/引擎接入/00-接入前现状审计.md》§5b（目标机实测）",
    aliases=("drawthings",),
    params_source="GET /sdapi/v1/options 实测 87 个键 ＋ Parameters.swift 的区间",
    home_env="",            # 不是仓库型运行时：地址在 engine_instance 表里，不在环境变量
    note="换模型只能在应用界面里做，平台不代切：引擎当前模型与登记行不符就阻断提交。"
         "实测 512×512、4 步约 15 秒；width/height 下限 128，缩略图不要走这条路。"
         "未识别的参数键会被 422 当场拒掉，不会被静默忽略。",
    models=(
        ModelSpec(
            code="noobai-xl-epsilon-11",
            label="noobaiXL（SDXL·epsilon 预测 1.1）",
            note="权重在应用自己的索引里，导入后改名成 *_f16.ckpt；登记行的"
                 "「权重路径」那一格填应用里显示的那个文件名，一致性核对靠它。",
            params=(
                Param("width", "int", 1024, 128, 8192,
                      note="128 是实测下限：送 64 会先撞 RangeValidator 报"
                           "「must be between 128 and 8192」"),
                Param("height", "int", 1024, 128, 8192),
                Param("steps", "int", 16, 1, 150,
                      note="默认值取自引擎当前模型的自述，切档会变——所以它是"
                           "「引擎说的默认」，不是平台定死的规格"),
                Param("guidance_scale", "float", 5, 0, 50,
                      note="别名 cfg_scale 同样收；白名单只登记这一个规范名"),
                Param("seed", "int", None, 0, 2147483647),
                Param("negative_prompt", "str", "",
                      note="2026-11-06 实测存在于返回体里；此前按 strings 命中形态"
                           "判为「不可定级」，现在由行为级确认"),
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
    params_source="`mlx_audio.tts.generate` 的实参表（2026-11-05 读 --help 取证）",
    note="执行走 mlx-audio＋MLX 8bit 权重；字级时间戳与拼音覆音尚未在这一支接上，"
         "所以口型仍由 Rhubarb 那条路单独做。",
    models=(
        ModelSpec(
            code="voxcpm2",
            label="VoxCPM2（MLX 8bit）",
            params=(
                Param("speed", "float", 1.0, 0.5, 2.0, note="语速倍率（--speed）"),
                Param("instruct", "str", "", note="文字造声描述（--instruct），留空用默认声"),
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
    params_source="`mlx_audio.music.generate` 的实参表（2026-11-05 读 --help 取证）",
    note="代码 Apache-2.0＋权重 MIT 两格分开登记；执行走 mlx-audio＋MLX 4bit。"
         "CLI 没有 --instrumental 这一档，纯音乐靠 caption 与 [inst] 段标记，"
         "所以这里不再声明一个 CLI 不接受的开关。",
    models=(
        ModelSpec(
            code="ace-step-1.5",
            label="ACE-Step 1.5（MLX 4bit）",
            params=(
                Param("duration", "int", 30, 10, 300, note="秒（--duration）"),
                Param("steps", "int", 27, 1, 100, note="流匹配步数（--steps）"),
                Param("seed", "int", None, 0, 2147483647),
                Param("lyrics", "str", "", note="结构化歌词；留空填 [inst]（CLI 必填）"),
            ),
        ),
    ),
)

# ---------- 文本 / 云端（W10 核验官方 schema 后登记） ----------
# 文本是本机唯一没有可用路线的能力（规格 02 §8.2 原文："没有合格的本地文本路线"）。
# DeepSeek 是国内官方 API、OpenAI 兼容、密钥名现场已存在，因此只登记这一家；
# 模型名与价格按 2026-11-01 官方文档核过，旧名 deepseek-chat/reasoner 已下架，
# 不再出现在清单里。v4-pro 的价格这次没有核到同一口径，宁可不登记。
_DEEPSEEK = Adapter(
    code="deepseek",
    label="DeepSeek（官方 API）",
    capability="text",
    backend="cloud",
    aliases=("deepseek-api",),
    key_env="DEEPSEEK_API_KEY",
    source="DeepSeek 官方文档（2026-11-01 核验）：https://api-docs.deepseek.com/",
    params_source="Chat Completions 官方参数表（max_tokens / temperature / thinking）",
    note="OpenAI 兼容端点；密钥只从环境变量名读取，平台不存值。",
    models=(
        ModelSpec(
            code="deepseek-flash",
            label="DeepSeek Flash",
            note="官方当前模型名。价格分忙闲时与输入/输出两段，界面只预填"
                 "输出忙时单价作为上限；实际用量由回执写进 resources。",
            params=(
                Param("max_tokens", "int", 2048, 1, 393216,
                      note="输出上限；文本作业按需调大"),
                Param("temperature", "float", None, 0, 2,
                      note="不填＝供应商默认"),
                Param("thinking", "bool", False,
                      note="开＝deepseek 的 thinking.type=enabled"),
            ),
            prices=(
                Price(
                    label="输出 token（忙时上限）",
                    unit="million_token",
                    currency="CNY",
                    amount_micros=8_000_000,
                    default_quantity=0.002,
                    source="DeepSeek 官方定价（2026-11-01）：输出 4 元/百万 token（闲时）、"
                           "8 元（忙时）；输入缓存命中 0.02/0.04、未命中 1/2",
                    note="按 2,000 token 预填；这是上限估算，不是最终结算价。",
                ),
            ),
        ),
    ),
)

# 火山方舟与 MiniMax 仍只登记名字：没有拿到同一口径的模型名/价格，不猜。
_TEXT_CLOUD = (
    _DEEPSEEK,
    Adapter(code="volc", label="火山方舟", capability="text", backend="cloud",
            aliases=("volces", "ark", "volcengine"),
            key_env="FV_KEY_VOLC", source="设计稿 §7.3（建议供应商）",
            note="一个端点挂多家模型；模型 ID 与参数到核官方文档再填。"),
    Adapter(code="minimax", label="MiniMax", capability="text", backend="cloud",
            key_env="FV_KEY_MINIMAX", source="设计稿 §7.3（候选供应商）",
            note="模型 ID 与参数到核官方文档再填。"),
)

# 云端视频：阿里云百炼 Wan 2.7 文生视频（异步任务）。
# 参数与价格按 2026-11-01 官方文档核验；base URL 随 Workspace 变，
# 所以清单只存环境变量名 ALIYUN_WAN_BASE_URL，不写死任何地址。
_WAN = Adapter(
    code="wan",
    label="阿里云百炼 Wan 2.7（视频）",
    capability="video",
    backend="cloud",
    aliases=("aliyun-wan", "dashscope-wan"),
    key_env="DASHSCOPE_API_KEY",
    source="阿里云百炼官方文档（2026-11-01 核验）：Wan 文生视频 API 与计费",
    params_source="Wan 2.7 文生视频 API 参数表（resolution / ratio / duration / seed）",
    note="异步任务：创建后轮询 /api/v1/tasks/{task_id}。base URL 用 "
         "ALIYUN_WAN_BASE_URL 给（带 Workspace 前缀），平台不猜地址。",
    models=(
        ModelSpec(
            code="wan2.7-t2v",
            label="Wan 2.7 文生视频",
            note="只接文生视频；图生视频与首尾帧不在本轮。resolution 默认 720P"
                 "是成本取舍（官方默认 1080P）。",
            params=(
                Param("resolution", "str", "720P", choices=("720P", "1080P")),
                Param("ratio", "str", "16:9",
                      choices=("16:9", "9:16", "1:1", "4:3", "3:4")),
                Param("duration", "int", 5, 2, 15, note="秒；按秒计费"),
                Param("seed", "int", None, 0, 2147483647),
            ),
            prices=(
                Price(
                    label="720P", unit="second", currency="CNY",
                    amount_micros=600_000, default_quantity=5,
                    when=(("resolution", "720P"),),
                    source="阿里云百炼官方价格（2026-11-01）：Wan 2.7 720P 0.6 元/秒",
                ),
                Price(
                    label="1080P", unit="second", currency="CNY",
                    amount_micros=1_000_000, default_quantity=5,
                    when=(("resolution", "1080P"),),
                    source="阿里云百炼官方价格（2026-11-01）：Wan 2.7 1080P 1 元/秒",
                ),
            ),
        ),
    ),
)

_VIDEO_CLOUD = (
    _WAN,
    *tuple(
        Adapter(code=code, label=label, capability="video", backend="cloud",
                key_env="FV_KEY_" + code.upper().replace("-", "_"),
                source="设计稿 §7.3 / §7.5 候选，未核验",
                note="云端视频必须逐镜头显式选择；模型档/计价/参数待核验。")
        for code, label in (("kling", "可灵"), ("seedance", "Seedance"))
    ),
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
    _FASTMETAL, _MLX_VIDEO, _MFLUX, _DRAW_THINGS, _VOXCPM, _ACE_STEP,
    *_TEXT_CLOUD, *_VIDEO_CLOUD, *_CLOUD_OTHER,
)

# 清单的版本号：Job 建单时冻结它，将来改清单能看出这条作业是按哪一版参数放行的。
# 2026-11-06.1＝W-B：新增本机服务型引擎 draw_things（执行形状与 mflux 那一族不同）。
MANIFEST_VERSION = "2026-11-06.1"

# 假适配器只给测试与演示用（config.ALLOW_MOCK 控制），不进清单。
# 它不执行任何命令，只按参数睡一会儿、写一个文本结果——这样队列语义可以
# 在没有模型权重的机器上被测穿，而不会假装 FastMetal 已经接通。
MOCK_PARAMS: tuple[Param, ...] = (
    Param("sleep", "float", 0.6, 0.0, 30.0, note="秒"),
    Param("fail", "bool", False, note="置 true 走失败分支"),
    Param("text", "str", "mock output", note="写进结果文件的文本"),
    # media 决定产物是**什么文件**。默认 text 只证明队列语义；mp4／png 让 worker
    # 用本机 ffmpeg 现造一段可播放的纯色片或一张图。为什么要这一档：
    # "生成 → 审核签字 → 时间线取片 → 合成 → 导出"这条链要求产物能被 ffprobe 读出
    # 视频流，文本文件走到底只会卡在合成前检查，业务验收就只能停在"作业成功了"。
    # 它仍然是假适配器——不做任何模型计算，只把产物做成真的媒体；
    # 因此 `model_validation` 依旧不给它记真实调用，production_ready 不亮。
    Param("media", "str", "text", choices=("text", "mp4", "png"),
          note="产物类型：text／mp4／png（后两者用本机 ffmpeg 现造）"),
    # 底色只给枚举，不给自由文本：lavfi 的 `color=c=<x>` 会拼进源串，
    # 收死成六色就不用在这里再写一套转义校验。
    Param("color", "str", "red",
          choices=("red", "green", "blue", "black", "white", "gray"),
          note="mp4／png 的底色"),
    Param("seconds", "float", 1.0, 0.2, 5.0, note="mp4 时长（秒）"),
)
MOCK_MODEL = "mock-1"

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


def _type_ok(kind: str, value) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        # bool 是 int 的子类，不单独排除的话 true 会被当成 1 混过去
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind in ("str", "image"):
        return isinstance(value, str) and value != ""
    return False


def _check_one(param: Param, value) -> list[str]:
    if not _type_ok(param.type, value):
        return [f"参数 {param.name} 的类型应为 {param.type}"]
    out: list[str] = []
    if param.choices and value not in param.choices:
        out.append(f"参数 {param.name} 只能是：{'、'.join(param.choices)}")
    if param.type in ("int", "float"):
        if param.minimum is not None and value < param.minimum:
            out.append(f"参数 {param.name} 不能小于 {param.minimum}")
        if param.maximum is not None and value > param.maximum:
            out.append(f"参数 {param.name} 不能大于 {param.maximum}")
        if param.multiple_of and param.multiple_of > 1 and value % param.multiple_of:
            out.append(f"参数 {param.name} 必须是 {param.multiple_of} 的倍数")
    return out


def validate_params(adapter: Adapter | None, model_code: str, params) -> list[str]:
    """按清单白名单校验参数；返回人话错误列表，空列表＝通过。

    未核验的云端 provider（models 为空）**不许带参数**：没有白名单就等于
    "模型名听着像"也能塞东西进去，规格 01-4.8.2 明令禁止。
    """
    if not isinstance(params, dict):
        return ["params 必须是一个对象"]
    if adapter is None:
        return ["provider 没有登记适配器，无法校验参数"]
    if not adapter.models:
        if params:
            return ["该 provider 的参数尚未核验，W07 不接受任何参数"]
        return []

    model = next((m for m in adapter.models if m.code == model_code), None)
    if model is None:
        known = "、".join(m.code for m in adapter.models)
        return [f"模型 {model_code} 不在清单里；可选：{known}"]

    spec = {p.name: p for p in model.params}
    errors: list[str] = []
    for name, value in params.items():
        param = spec.get(name)
        if param is None:
            errors.append(f"参数 {name} 不在 {model.code} 的白名单里")
        else:
            errors.extend(_check_one(param, value))
    for param in model.params:
        if param.type == "image" and param.name not in params:
            errors.append(f"缺少必填参数：{param.name}")
    return errors


def validate_mock_params(params) -> list[str]:
    """假适配器自己的白名单；它不进清单，所以单独校验。"""
    if not isinstance(params, dict):
        return ["params 必须是一个对象"]
    spec = {p.name: p for p in MOCK_PARAMS}
    errors: list[str] = []
    for name, value in params.items():
        param = spec.get(name)
        if param is None:
            errors.append(f"参数 {name} 不在 mock 的白名单里")
        else:
            errors.extend(_check_one(param, value))
    return errors


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

    # 本地不再只看"目录在不在"：W09 真要起子进程，解释器、入口脚本、
    # 权重四件套缺一件都得提前说清。runtime.py 只做文件系统检查，不加载权重。
    base["key_env"] = ""
    model = (row["model"] or "").strip()
    if not model:
        return {**base, "state": "missing", "detail": "选了 provider 但还没填模型档"}
    if adapter.code in engines.SERVICE_ENGINES:
        # 本机服务型引擎（Draw Things 这一类）没有"仓库四件套"可查：它的真相是
        # 端点连不连得上、引擎当前选的是哪一档权重。探测住在 engines.py 而不是
        # runtime.py——后者的模块头明写着"不发网络请求"，那条边界正是设置页
        # 敢随手调用它的前提，破了它"点开一页设置会不会联网"就变成要看代码才知道。
        checked = engines.probe(adapter.code)
        return {**base, "state": checked.state, "detail": checked.detail,
                "engine_model": checked.values.get("model") or "",
                "endpoint": checked.values.get("endpoint") or ""}
    checked = runtime.probe(adapter.code, model)
    return {**base, "state": checked.state, "detail": checked.detail}


# 选路策略的**唯一出处**。原来这四行写在 `manifest_payload()` 的函数体里，
# 于是任何想在别处念一句"到底会不会自动切"的代码都只能自己再写一遍字面量——
# 模型中心的路由就这么抄了一份 `"no_auto_fallback": True`，两份一起漂移的可能
# 恰恰存在于这种"看着不像配置、其实是口径"的地方。
POLICY = {
    "local_default_capabilities": ["image", "video", "tts", "music"],
    "video_cloud_must_be_explicit": True,
    "no_auto_fallback": True,
    "note": "云端视频只能逐镜头显式选择；本地失败不会自动转云端。",
}


def manifest_payload() -> dict:
    """给界面和 W07 的唯一清单出口。"""
    return {
        "policy": POLICY,
        "adapters": [a.as_dict() for a in ADAPTERS],
    }
