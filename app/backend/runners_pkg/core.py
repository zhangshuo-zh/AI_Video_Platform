"""执行器 core 域。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import adapters
import engines
import runtime
from config import FILES_DIR, JOBS_DIR
from paths import resolve_within, within

from .audio import ACE_STEP_INSTRUMENTAL as ACE_STEP_INSTRUMENTAL
from .audio import ACE_STEP_MLX_MODEL as ACE_STEP_MLX_MODEL
from .audio import VOXCPM_MLX_MODEL as VOXCPM_MLX_MODEL
from .audio import _ace_step as _ace_step
from .audio import _voxcpm as _voxcpm
from .cloud import _cloud as _cloud
from .cloud import _secret_envs as _secret_envs
from .common import _flag as _flag
from .common import _metrics_path as _metrics_path
from .common import _mp4_path as _mp4_path
from .common import _need_runtime as _need_runtime
from .common import _spec_default as _spec_default
from .image import DRAW_THINGS_PATH as DRAW_THINGS_PATH
from .image import _comfyui as _comfyui
from .image import _comfyui_workflow_payload as _comfyui_workflow_payload
from .image import _draw_things as _draw_things
from .image import _mflux as _mflux
from .inputs import _asset_project_of as _asset_project_of
from .inputs import _is_platform_artifact as _is_platform_artifact
from .inputs import _is_project_asset_file as _is_project_asset_file
from .inputs import _job_dir_root as _job_dir_root
from .inputs import _job_project_of as _job_project_of
from .inputs import _resolve_input_image as _resolve_input_image
from .protocol import CLOUD_RUNNER as CLOUD_RUNNER
from .protocol import ENGINE_HTTP_RUNNER as ENGINE_HTTP_RUNNER
from .protocol import LLM_RUNNER as LLM_RUNNER
from .protocol import RunnerUnavailable as RunnerUnavailable
from .protocol import RunSpec as RunSpec
from .results import _magic_ok as _magic_ok
from .results import cleanup_partial as cleanup_partial
from .results import metrics_summary as metrics_summary
from .results import provider_receipt as provider_receipt
from .results import verify as verify
from .text import _ollama as _ollama
from .video import _fastmetal as _fastmetal
from .video import _mlx_video as _mlx_video


def build(job, params: dict, outputs: Path) -> RunSpec:
    """按能力与 provider 分发。没接线的组合明确拒绝，不找替代品。

    `execution_mode` 是 Job 行冻结的：本地作业永远走本地分支，
    云端作业才走 `_cloud`。这里没有"失败就换一条轨"的入口。
    """
    capability = job["capability"]
    provider = job["provider_code"]
    model = job["model_code"]
    mode = job["execution_mode"]
    if mode == "cloud":
        adapter = adapters.find(capability, "cloud", provider)
        if adapter is None:
            raise RunnerUnavailable(f"provider「{provider}」没有登记云端适配器")
        if not adapter.models:
            raise RunnerUnavailable(f"{provider} 的参数与模型尚未核验，不能执行")
        return _cloud(job, params, outputs, adapter)
    if capability == "video" and provider == "fastmetal":
        return _fastmetal(job, params, outputs, model)
    if capability == "video" and provider == "mlx-video":
        return _mlx_video(job, params, outputs)
    if capability == "image" and provider == "mflux":
        return _mflux(job, params, outputs)
    if capability == "image" and provider in ("draw_things", "drawthings"):
        return _draw_things(job, params, outputs, model)
    if capability == "image" and provider in ("comfyui", "comfy", "comfy-desktop"):
        return _comfyui(job, params, outputs, model)
    if capability == "text" and provider in ("ollama",):
        return _ollama(job, params, outputs, model)
    if capability == "tts" and provider in ("voxcpm2", "voxcpm"):
        return _voxcpm(job, params, outputs)
    if capability == "music" and provider in ("ace-step", "acestep", "ace_step"):
        return _ace_step(job, params, outputs)
    raise RunnerUnavailable(f"{capability}/{provider} 还没有执行接线")




def has_branch(capability: str, provider: str) -> bool:
    """这一对 capability/provider 在 `build()` 里有没有分支。

    存在的理由：音频中心那张能力表原来把 tts/music 的 `runner_wired` **写死成 False**，
    注释还写着"今天没有分支"——那是一句会在接线当天就变成假话的实话。
    现在它问这里，而这里与 `build()` 读的是同一串条件。
    """
    if capability == "text":
        return provider == "ollama"
    if capability == "video":
        return provider in ("fastmetal", "mlx-video")
    if capability == "image":
        return provider in ("mflux", "draw_things", "comfyui")
    if capability == "tts":
        return provider in ("voxcpm2", "voxcpm")
    if capability == "music":
        return provider in ("ace-step", "acestep", "ace_step")
    return False



__all__ = [
    'annotations',
    'json',
    'os',
    'sys',
    'dataclass',
    'field',
    'Path',
    'adapters',
    'engines',
    'runtime',
    'FILES_DIR',
    'JOBS_DIR',
    'resolve_within',
    'within',
    'CLOUD_RUNNER',
    'ENGINE_HTTP_RUNNER',
    'LLM_RUNNER',
    'RunnerUnavailable',
    'RunSpec',
    '_need_runtime',
    '_spec_default',
    '_flag',
    '_mp4_path',
    '_metrics_path',
    '_fastmetal',
    '_job_dir_root',
    '_is_platform_artifact',
    '_job_project_of',
    '_asset_project_of',
    '_is_project_asset_file',
    '_resolve_input_image',
    '_mlx_video',
    '_mflux',
    'DRAW_THINGS_PATH',
    '_comfyui_workflow_payload',
    '_comfyui',
    '_draw_things',
    '_ollama',
    'VOXCPM_MLX_MODEL',
    'ACE_STEP_MLX_MODEL',
    'ACE_STEP_INSTRUMENTAL',
    '_voxcpm',
    '_ace_step',
    '_secret_envs',
    '_cloud',
    'build',
    'has_branch',
    '_magic_ok',
    'verify',
    'provider_receipt',
    'metrics_summary',
    'cleanup_partial',
]
