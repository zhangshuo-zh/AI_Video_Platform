"""适配器规格。修改规格时在对应能力模块维护。"""
from __future__ import annotations
from .types import Adapter, ModelSpec, Param


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
                Param("init_image", "image", None,
                      note="参考底图（图生图；留空时走文生图，非空自动切换 img2img）",
                      required=False),
                Param("strength", "float", 0.65, 0.05, 1.0,
                      note="图生图重绘强度（denoising_strength）",
                      required=False),
            ),
        ),
    ),
)


# ---------- 图像：本机 ComfyUI（**只接探测，不接执行**，2026-11-13）----------
# 为什么它在这里、却 models=() 空着：
#
# ComfyUI 的核心是**参数化 workflow JSON**（`/prompt` 收的是一整张节点图），
# 而平台的执行形状只有两种——argv 子进程（`runners.build()` 的分支）或一次性
# HTTP 请求（`cloud_run.py`）。第二形态最接近，但它要求"参数 → 请求体"是一张
# 平坦映射；ComfyUI 要的是把参数注入到图里的具名节点（KSampler.seed 之类），
# 那是 workflow 契约，不是参数白名单。没有这份契约就写 `models=`，等于拿一份
# 猜的参数表去承诺"这一档能出图"。
#
# 所以这一条只做**探测器**：`SERVICE_ENGINES` 认它，`_INFO_PATHS` 给它
# `/system_stats`（本机/云端同名，见下），引擎页能看见它通不通；`runners.build()`
# 里**没有**它的分支，`has_branch()` 也回 False，提交框列不到它。
#
# 端点选择：`/system_stats`。官方路由文档（docs.comfy.org「服务器路由」）把它列在
# 核心 API 第一条，用途正是"取系统信息（Python 版本、设备、显存）"；实测形态是
# `{"system": {"comfyui_version", "python_version", "devices": [...]}}`，
# 显存在 `system.devices[0].vram_total`／`vram_free`（老版本落在
# `system.memory.total/free`，`_describe()` 两处都认）。选它而不选 `/object_info`：
# 后者是"这台机器装了哪些节点"，几 MB 的 JSON，用来判"服务在不在"太贵。
_COMFYUI = Adapter(
    code="comfyui",
    label="ComfyUI（工作流节点）",
    capability="image",
    backend="local",
    source="官方路由文档 docs.comfy.org「服务器路由（HTTP 和 WebSocket API）」的"
           "核心 API 表：/system_stats、/object_info、/queue；本机装的是 "
           "Comfy Desktop 1.1.8（/Applications/Comfy Desktop.app，bundle "
           "com.todesktop.241012ess7yxs0e），默认端口 8188",
    aliases=("comfy", "comfy-desktop"),
    params_source="ComfyUI /prompt API Workflow + KSampler/CLIPTextEncode/LoadImage 参数注入",
    home_env="",
    note="支持参数化 ComfyUI API Workflow JSON 模板执行：可传入自定义工作流节点图"
         "（支持 {{prompt}} / {{negative_prompt}} / {{seed}} / {{steps}} / {{width}} / "
         "{{height}} / {{cfg}} / {{denoise}} / {{checkpoint}} / {{image}} 占位符）"
         "或使用默认标准文生图/图生图骨架，经由 /prompt → /history → /view 回收产物。",
    models=(
        ModelSpec(
            code="comfyui-workflow",
            label="ComfyUI API Workflow（节点图模板）",
            note="提交参数化节点图给 ComfyUI；workflow_json 留空时自动生成默认节点图。",
            params=(
                Param("workflow_json", "str", "",
                      note="ComfyUI API 格式 JSON 节点图模板（支持占位符注入）",
                      required=False),
                Param("checkpoint", "str", "",
                      note="CheckpointLoaderSimple.ckpt_name 覆盖名",
                      required=False),
                Param("width", "int", 1024, 256, 4096, 8),
                Param("height", "int", 1024, 256, 4096, 8),
                Param("steps", "int", 20, 1, 150),
                Param("cfg", "float", 7.0, 0.5, 30.0),
                Param("seed", "int", None, 0, 2147483647),
                Param("negative_prompt", "str", "",
                      note="负向提示词",
                      required=False),
                Param("image", "image", None,
                      note="参考底图（填入时自动切换为 LoadImage 图生图节点图）",
                      required=False),
                Param("denoise", "float", 0.75, 0.05, 1.0,
                      note="图生图重绘强度（KSampler.denoise）",
                      required=False),
            ),
        ),
    ),
)
