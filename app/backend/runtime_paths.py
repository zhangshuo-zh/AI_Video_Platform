"""把"网页上设的运行时路径"应用到进程环境，并说清哪些改动要重启 worker（0051）。

为什么单开一层而不是塞进 `repositories/runtime_path`：
仓储层要保持无副作用（`get_all`/`save` 只碰库）。把值塞进 `os.environ`
有全局副作用、还要决定"谁需要重启"，那是另一个职责。分开之后，
存储层可以放心被探测路径高频调用，而这一层只在人点保存时跑一次。

三条与这个功能绑定的判据：

1. **环境变量优先，所以"能不能改"取决于环境里有没有**。
   如果部署时显式 `export FASTVIDEO_FASTMETAL_HOME=...`，网页上设的值会被
   静默无视——那不是 bug，是"显式配置优先"的必然。所以 `describe()` 必须
   把"这一项被环境变量占住了，页面改不动"直接说出来，否则人改了没反应
   会以为是平台坏了。

2. **API 进程与 worker 是两个进程**。`os.environ` 改了只对 API 生效；
   worker 拿到的是 `_worker_env()` 在 **fork 那一刻** 的快照。
   所以"改完要重启 worker"是硬约束，不是疏漏。

3. **不自动重启**。重启 worker 会打断正在跑的作业（`_terminate` 会让作业
   记成 `worker_stopped` 可重试）。这件事必须由人点，不能因为"顺手"就在
   保存路径时把人家跑着的 12 分钟视频掐掉。
"""
from __future__ import annotations

import os

from repositories import runtime_path

# 每一项的人话说明。页面直接用它，不在前端再抄一份（抄一份就会分叉）。
LABELS: dict[str, tuple[str, str]] = {
    "FASTVIDEO_FASTMETAL_HOME": ("FastMetal", "视频主力引擎的运行时根目录"),
    "FASTVIDEO_FASTMETAL_MODEL": ("FastMetal 模型", "覆盖权重目录（不给则用 home 下的默认）"),
    "FASTVIDEO_MLX_VIDEO_HOME": ("MLX 视频", "视频备选引擎的运行时根目录"),
    "FASTVIDEO_MLX_VIDEO_MODEL": ("MLX 视频模型", "转换后的 Wan2.2 I2V 目录"),
    "FASTVIDEO_MFLUX_HOME": ("mflux", "图像引擎的运行时根目录"),
    "FASTVIDEO_MFLUX_BIN": ("mflux 可执行文件", "直接指向 mflux-generate"),
    "FASTVIDEO_VOXCPM_HOME": ("VoxCPM", "配音引擎的运行时根目录"),
    "FASTVIDEO_ACE_STEP_HOME": ("ACE-Step", "音乐引擎的运行时根目录"),
    "FASTVIDEO_MLX_AUDIO_BIN": ("MLX 音频可执行文件", "直接指向音频 CLI"),
    "FASTVIDEO_RIFE_WEIGHTS": ("RIFE 权重", "插帧模型目录；不给则找 HF 缓存"),
}


def _label(env_name: str) -> tuple[str, str]:
    return LABELS.get(env_name, (env_name, ""))


def describe() -> list[dict]:
    """每一项的三份值，页面靠它把"为什么改了没生效"说清楚。

    返回的每一项：
      * `env_name`   环境变量名（主键）
      * `label`/`hint` 人话名称与说明
      * `env_value`  **进程环境里**的值（None = 没在环境里配）
      * `saved_value` 网页上保存的值（None = 没设过）
      * `effective`  当前**这个进程**实际会用的值（环境优先）
      * `source`     `env` / `web` / `default` —— 值从哪来
      * `locked`     True 表示被环境变量占住，页面改不动（写进库也不生效）
    """
    saved = runtime_path.get_all()
    out: list[dict] = []
    for env_name in runtime_path.ALLOWED_ENV:
        label, hint = _label(env_name)
        env_value = (os.environ.get(env_name) or "").strip() or None
        saved_value = saved.get(env_name)
        if env_value:
            effective, source = env_value, "env"
        elif saved_value:
            effective, source = saved_value, "web"
        else:
            effective, source = None, "default"
        out.append({
            "env_name": env_name,
            "label": label,
            "hint": hint,
            "env_value": env_value,
            "saved_value": saved_value,
            "effective": effective,
            "source": source,
            "locked": bool(env_value),
        })
    return out


def apply() -> list[str]:
    """把库里设过的值写进当前进程环境（API 进程用）。

    只管"库里有、环境里没有"的那些：环境里已有的不覆盖——那是显式配置，
    优先级高于网页。返回真正被写进去的变量名，便于日志与测试断言。

    **不做危险判定**：路径存不存在由 `runtime` 探测回答（missing/unknown），
    这一层不因为"目录不在"就拒绝，因为网络盘挂载慢、目录稍后才出现是正常的。
    """
    written: list[str] = []
    for env_name, path in runtime_path.get_all().items():
        if (os.environ.get(env_name) or "").strip():
            continue                     # 环境里有，网页的不生效
        os.environ[env_name] = path
        written.append(env_name)
    return written


def apply_one(env_name: str, path: str | None) -> None:
    """保存单项之后立刻让**这个 API 进程**看到新值（或回到无覆盖）。

    注意：这只影响 API 进程自己的探测结果与健康检查。**worker 不会跟着变**，
    它要重启才会拿到新环境——页面上必须提示这一点。
    """
    if (os.environ.get(env_name) or "").strip():
        return                            # 被环境占住，网页值不生效
    if path:
        os.environ[env_name] = path
    else:
        os.environ.pop(env_name, None)
