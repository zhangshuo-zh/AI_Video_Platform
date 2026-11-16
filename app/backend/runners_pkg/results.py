"""执行器 results 域。"""
from __future__ import annotations

import json
from pathlib import Path

from .protocol import RunnerUnavailable, RunSpec


def _magic_ok(path: Path, media: str) -> bool:
    try:
        head = path.open("rb").read(16)
    except OSError:
        return False
    if media == "video":
        # ISO-BMFF: 4 字节长度 + 'ftyp'。FastMetal 与 ffmpeg 都产出这个头。
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if media == "image":
        return head.startswith(b"\x89PNG\r\n\x1a\n") or head[:3] == b"\xff\xd8\xff"
    if media == "audio":
        # WAV＝RIFF....WAVE；mp3＝ID3；flac＝fLaC；m4a 走 ISO-BMFF。
        # 这条不是装饰：音频产物一旦被截断、或写成一张 HTML 错误页，
        # 时间线与成片会带着一个"看起来存在"的空音轨走下去。
        return (head[:4] == b"RIFF" and head[8:12] == b"WAVE") or \
            head[:3] == b"ID3" or head[:4] == b"fLaC" or \
            (len(head) >= 12 and head[4:8] == b"ftyp")
    return True




def verify(spec: RunSpec) -> list[Path]:
    """按媒体类型验真；返回实际存在的产物路径。缺一件就抛，不写半条成功。"""
    # `RunSpec` 是 frozen 的：找到的产物只带出去，不回头改那份规格。
    expected = list(spec.expected)
    if not expected and spec.expected_glob:
        expected = sorted(p for p in spec.cwd.glob(spec.expected_glob) if p.is_file())
        if not expected:
            raise RunnerUnavailable(
                "命令返回了，但 %s 里没有任何 %s 产物"
                % (spec.cwd.name, spec.expected_glob))
    missing = [str(p) for p in expected if not p.is_file()]
    if missing:
        raise RunnerUnavailable("命令返回了，但没有产物：" + "、".join(Path(p).name for p in missing))
    for path in expected:
        if path.stat().st_size <= 0:
            raise RunnerUnavailable(f"产物是空的：{path.name}")
        if not _magic_ok(path, spec.media):
            raise RunnerUnavailable(f"产物不是可识别的 {spec.media} 文件：{path.name}")
    return expected




def provider_receipt(spec: RunSpec) -> dict:
    """读 cloud_run.py 留下的回执，只留对账需要的字段。

    回执里可能有完整错误响应；这里按白名单挑，既避免把供应商原文
    整段塞进 /api/jobs 的响应，也保证 `request_id` 与 `usage` 一定在。
    """
    path = Path(spec.meta.get("result_path", ""))
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    keep = {}
    for key in ("ok", "provider", "model", "capability", "request_id",
                "task_request_id", "task_status", "http_status", "latency_ms",
                "bytes", "usage", "response_meta", "error"):
        if key in raw:
            keep[key] = raw[key]
    return keep




def metrics_summary(path: Path) -> dict:
    """从模型自己写的 metrics.json 里挑几项进 resources；原文件仍留在 outputs。

    不是整份搬进库：metrics 里有模型路径、完整调度表，既不必要也不该
    进 /api/jobs 的响应。
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    keep = {}
    for key in ("prompt", "height", "width", "num_frames", "seed",
                "decode_backend", "spatial_mode", "fast", "refine",
                "prompt_encode_s", "mlx_dit_load_s", "mlx_denoise_s",
                "vae_decode_export_s", "total_s", "dmd_denoising_steps"):
        if key in raw:
            keep[key] = raw[key]
    for src, dst in (("mlx_denoise_peak_bytes", "denoise_peak_gb"),
                     ("mlx_load_peak_bytes", "load_peak_gb")):
        value = raw.get(src)
        if isinstance(value, (int, float)) and value >= 0:
            keep[dst] = round(value / (1024 ** 3), 2)
    return keep




def cleanup_partial(spec: RunSpec) -> None:
    """失败时删掉半截产物，免下次重试被旧文件骗过。"""
    for path in spec.expected:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
