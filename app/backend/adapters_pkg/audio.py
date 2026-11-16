"""适配器规格。修改规格时在对应能力模块维护。"""
from __future__ import annotations
from .types import Adapter, ModelSpec, Param


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
