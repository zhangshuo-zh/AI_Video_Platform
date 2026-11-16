"""适配器规格。修改规格时在对应能力模块维护。"""
from __future__ import annotations
from .types import Adapter, ModelSpec, Param, Price



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
                Param("image", "image", None, note="必填：条件图（首帧）路径由 Job 的输入产物给"),
                Param("last_frame_image", "image", None, required=False,
                      note="选填：尾帧参考图路径（首尾帧过渡生视频）"),
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
