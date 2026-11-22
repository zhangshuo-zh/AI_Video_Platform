"""服务器/本机硬件资源与平台产物缓存只读采样（纯标准库，跨 macOS / Linux）。

不返回任何绝对文件路径、环境变量或主机名，仅暴露百分比与字节容量，
供所有已登录用户查看硬件水位并协助向管理员反馈问题。
"""
from __future__ import annotations

import math
import os
import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

import diskplan
import jobfiles

T = TypeVar("T")


class _SampleCache(Generic[T]):
    """进程内单次刷新。过期时其他读者可读上一帧，失效后须等新帧。"""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self.condition = threading.Condition()
        self.generation = 0
        self.building = False
        self.value: T | None = None
        self.expires = 0.0

    def invalidate(self) -> None:
        with self.condition:
            self.generation += 1
            self.value = None

    def get(self, build: Callable[[], T]) -> T:
        while True:
            with self.condition:
                if self.value is not None and (self.building or time.monotonic() < self.expires):
                    return self.value
                if self.building:
                    self.condition.wait()
                    continue
                self.building = True
                generation = self.generation
            try:
                result = build()
                with self.condition:
                    if generation == self.generation:
                        self.value = result
                        self.expires = time.monotonic() + self.ttl
                        return result
            finally:
                with self.condition:
                    self.building = False
                    self.condition.notify_all()


_HARDWARE: _SampleCache[dict] = _SampleCache(1.5)
_SPACE: _SampleCache[tuple[dict, dict]] = _SampleCache(5.0)
_LAST_CPU_SAMPLE: tuple[int, int] | None = None
_SIZE_UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def invalidate_cache() -> None:
    """GC 完成后废弃磁盘快照及进行中的旧扫描。"""
    _SPACE.invalidate()


def _run_cmd(args: list[str], timeout: float = 1.5) -> str:
    try:
        return subprocess.check_output(args, encoding="ascii", errors="replace",
                                       stderr=subprocess.DEVNULL, timeout=timeout,
                                       env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return ""


def _sample_cpu_linux() -> float | None:
    global _LAST_CPU_SAMPLE
    try:
        with Path("/proc/stat").open(encoding="ascii", errors="replace") as stream:
            parts = stream.readline().split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        # guest / guest_nice 已计入 user / nice。
        nums = [int(field) for field in parts[1:9]]
    except (OSError, ValueError):
        return None
    if min(nums) < 0:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums)
    # 只有硬件缓存的刷新线程会写这对计数。
    prev = _LAST_CPU_SAMPLE
    _LAST_CPU_SAMPLE = (total, idle)
    if prev is None or total <= prev[0] or not 0 <= idle - prev[1] <= total - prev[0]:
        return None
    d_total = total - prev[0]
    d_idle = idle - prev[1]
    return round((1.0 - d_idle / d_total) * 100.0, 1)


def _sample_cpu(sys_name: str, cores: int, load_1m: float) -> float:
    if sys_name == "Linux":
        pct = _sample_cpu_linux()
        if pct is not None:
            return pct
    elif sys_name == "Darwin":
        out = _run_cmd(["ps", "-A", "-o", "%cpu"])
        if out:
            vals: list[float] = []
            for ln in out.splitlines()[1:]:
                s = ln.strip()
                if not s:
                    continue
                try:
                    value = float(s)
                    if math.isfinite(value) and value >= 0:
                        vals.append(value)
                except ValueError:
                    continue
            if vals:
                return round(max(0.0, min(100.0, sum(vals) / max(1, cores))), 1)
    # 回退：按 1 分钟负载与核数换算
    return round(max(0.0, min(100.0, (load_1m / max(1, cores)) * 100.0)), 1)


def _usage(total: int, used: int) -> dict:
    used = max(0, min(total, used))
    return {"total_bytes": total, "used_bytes": used,
            "percent": round(100 * used / total, 1) if total else 0.0}


def _swap_bytes(output: str, field: str) -> int:
    token = re.search(rf"\b{field}\s*=\s*(\d{{1,16}}(?:\.\d+)?)\s*([BKMGT])(?:i?B)?\b",
                      output, re.IGNORECASE)
    return int(float(token[1]) * _SIZE_UNITS[token[2].upper()]) if token else 0


def _sample_memory_darwin() -> tuple[dict, dict]:
    mem_raw = _run_cmd(["sysctl", "-n", "hw.memsize"]).strip()
    total_bytes = int(mem_raw) if mem_raw.isdigit() else 0

    used_bytes = 0
    vm_out = _run_cmd(["vm_stat"])
    if vm_out:
        m_page = re.search(r"page size of (\d+) bytes", vm_out)
        page_size = int(m_page.group(1)) if m_page else 0
        counts: dict[str, int] = {}
        for ln in vm_out.splitlines():
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            digits = v.strip().rstrip(".")
            if digits.isdigit():
                counts[k.strip()] = int(digits)
        active = counts.get("Pages active", 0)
        wired = counts.get("Pages wired down", 0)
        compressed = counts.get("Pages occupied by compressor", 0)
        used_bytes = (active + wired + compressed) * page_size

    swap = _run_cmd(["sysctl", "-n", "vm.swapusage"])
    return _usage(total_bytes, used_bytes), _usage(_swap_bytes(swap, "total"), _swap_bytes(swap, "used"))


def _sample_memory_linux() -> tuple[dict, dict]:
    info: dict[str, int] = {}
    try:
        for ln in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in ln:
                continue
            k, rest = ln.split(":", 1)
            parts = rest.strip().split()
            if len(parts) == 2 and parts[1] == "kB" and parts[0].isascii() and parts[0].isdigit():
                info[k.strip()] = int(parts[0]) * 1024
    except OSError:
        return _usage(0, 0), _usage(0, 0)
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", total))
    swap = info.get("SwapTotal", 0)
    return _usage(total, total - avail), _usage(swap, swap - info.get("SwapFree", swap))


def _sample_cache_and_disk() -> tuple[dict, dict]:
    vol = jobfiles.volume_usage()
    disk = _usage(vol.total, vol.used) if vol else _usage(0, 0)
    disk["free_bytes"] = vol.free if vol else 0
    return disk, diskplan.cache_usage()


def _sample_hardware() -> dict:
    cores = os.cpu_count() or 1
    try:
        load_1m = round(os.getloadavg()[0], 2)
    except OSError:
        load_1m = 0.0
    system = platform.system()
    cpu_pct = _sample_cpu(system, cores, load_1m)
    mem, swap = _sample_memory_darwin() if system == "Darwin" else _sample_memory_linux()
    return {
        "cpu": {"percent": cpu_pct, "cores": cores, "load_1m": load_1m},
        "memory": mem,
        "swap": swap,
        "platform": system if system in ("Linux", "Darwin") else "Unknown",
        "observed_at": int(time.time()),
    }


def snapshot(*, can_clear: bool = False) -> dict:
    """返回主机资源，权限位仅在拼装当前请求的响应时加入。"""
    hardware = _HARDWARE.get(_sample_hardware)
    disk, cache = _SPACE.get(_sample_cache_and_disk)
    return {**hardware, "disk": disk, "cache": {**cache, "can_clear": can_clear}}
