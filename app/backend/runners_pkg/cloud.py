"""执行器 cloud 域。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import adapters

from .protocol import CLOUD_RUNNER, RunnerUnavailable, RunSpec


def _secret_envs(request: dict) -> tuple[str, ...]:
    """这个请求需要子进程看得见哪几个**变量名**（不是值）。

    三个 runner（cloud / engine_http / llm）都是同一个设计：
    请求 JSON 里只放变量名，值只存在进程环境里，由子进程自己 `os.environ.get`。
    所以 worker 的白名单必须按名字放行——否则 `Authorization` 拼不出来，
    调用会退化成 `secret_env_missing`。

    为什么从 request 里读而不是写死：变量名是**每个引擎实例各自配置**的
    （`engine_instance.shared_secret_env`），写死等于把实例配置架空。
    """
    names = []
    for key in ("key_env", "secret_env"):
        name = (request.get(key) or "").strip()
        if name and name not in names:
            names.append(name)
    # 基址覆盖也是子进程要用的非密钥配置（`cloud_run` 里读）
    for key in ("DEEPSEEK_BASE_URL", "ALIYUN_WAN_BASE_URL"):
        if os.environ.get(key):
            names.append(key)
    return tuple(names)




def _cloud(job, params: dict, outputs: Path, adapter) -> RunSpec:
    """云端：把一次调用写成请求 JSON，交给 cloud_run.py 子进程。

    argv 里只有请求文件路径；密钥值不进来，日志里也没有可泄露的东西。
    base URL 一律从环境变量读，清单里只留变量名。
    """
    key_env = adapters.resolved_key_env(adapter.code, adapter.capability, "cloud")
    if not adapters.key_present(key_env):
        raise RunnerUnavailable(f"环境变量 {key_env} 没有值；云端作业不能起")

    capability = job["capability"]
    if capability == "text":
        ext, media = "md", "text"
    elif capability == "video":
        ext, media = "mp4", "video"
    else:
        raise RunnerUnavailable(f"{capability} 云端执行还没有接线")

    request_path = outputs / f"{job['id']}-cloud-request.json"
    result_path = outputs / f"{job['id']}-cloud-result.json"
    out = outputs / f"{job['id']}-{adapter.code}.{ext}"
    request = {
        "provider": adapter.code,
        "capability": capability,
        "model": job["model_code"],
        "prompt": job["prompt_snapshot"],
        "params": params,
        "key_env": key_env,
        "output": str(out),
        "result": str(result_path),
    }
    if adapter.code == "wan":
        if not (os.environ.get("ALIYUN_WAN_BASE_URL") or "").strip():
            raise RunnerUnavailable(
                "没有配置 ALIYUN_WAN_BASE_URL（要带 Workspace 前缀的官方地址）")
        request["poll_interval"] = 5
        request["poll_timeout"] = 1800
    else:
        request["http_timeout"] = 120
    # 请求文件里没有任何密钥值；key_env 只是变量名。
    # 写完立刻落盘，子进程只认这一份快照，运行中被改环境也不影响这次调用。
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    argv = [sys.executable, str(CLOUD_RUNNER), str(request_path)]
    return RunSpec(
        runner=f"cloud:{adapter.code}", argv=argv, cwd=outputs,
        expected=[out], media=media,
        # 云端 runner 要把 `key_env` 点名的那个密钥读出来拼 Authorization。
        # 白名单模式下必须显式声明，否则子进程里根本看不到它。
        pass_env=_secret_envs(request),
        meta={"adapter": adapter.code, "model": job["model_code"],
              "provider": adapter.code, "request_path": str(request_path),
              "result_path": str(result_path)})
