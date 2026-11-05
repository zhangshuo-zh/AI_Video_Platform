"""在 FastVideo 的虚拟环境里跑的一段补帧脚本（**不由平台进程直接 import**）。

为什么单独成一个文件而不是写进 worker：RIFE 的 MLX 后端住在
`$FASTVIDEO_FASTMETAL_HOME/.venv` 那个环境里（fastvideo、mlx、imageio 都在那边），
平台的 venv 里没有这些包；把它们装进平台 venv 等于让 Web 服务的依赖跟着
模型运行时一起膨胀。所以这一侧只收命令行参数、只做一件事、把结果打成一行 JSON
交回给 `composition/interpolate.py`。

为什么输出 PNG 序列而不是直接写 mp4：合成产物已经是一次 h264 有损编码，
如果这里再编码一次 mp4 给 ffmpeg 二次编码，画面就要过**三代**有损。
PNG 是无损的，后面那一步 `ffmpeg` 只编一次，最终文件是两代——
和上游 `mlx_wan_prompt_to_video.py` 的 `_postprocess_video` 相比也少一代。

帧数为什么按 fps 换算而不是按 factor 翻倍：`interpolate_to_frame_count`
接受非整数倍目标（首尾帧原样保留，中间帧按均匀时间位置求值），
16→24 这种 3:2 正好用它；上游那条 `--fast` 路径是为"少生成再补全"设计的，
这里是"已经生成完、只补时间轴"，两者不能混用同一个 factor 语义。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="RIFE 补帧：把一段视频补到目标帧数")
    parser.add_argument("--input", required=True, help="要补帧的视频（合成产物）")
    parser.add_argument("--frames-dir", required=True,
                        help="PNG 序列的输出目录（调用方负责清理）")
    parser.add_argument("--source-fps", type=float, required=True,
                        help="输入文件的实际帧率（ffprobe 现读，不是时间线里写的期望值）")
    parser.add_argument("--target-fps", type=float, required=True,
                        help="要补到的帧率")
    parser.add_argument("--expect-source-frames", type=int, default=0,
                        help="期望的源帧数；给 0 表示不校验。对不上就退出，"
                             "不默默补出一个少几帧的成片")
    parser.add_argument("--model-version", default="4.25")
    parser.add_argument("--weights-dir", default=None,
                        help="RIFE 权重目录；留空按缓存里的 mlx-community/RIFE-4.25")
    args = parser.parse_args()

    src = Path(args.input)
    frames_dir = Path(args.frames_dir)
    if not src.is_file():
        return _fail("input_missing", f"要补帧的文件不在：{src.name}")
    if args.source_fps <= 0 or args.target_fps <= 0:
        return _fail("fps_invalid",
                     f"帧率不合法：源 {args.source_fps}／目标 {args.target_fps}")

    try:
        import imageio.v3 as iio
        import numpy as np  # noqa: F401  （interpolate 内部按 ndarray 校验形状）
    except ImportError as exc:
        return _fail("venv_deps_missing",
                     f"FastVideo 环境里缺包：{exc}；用该环境的 python 跑这段脚本")

    try:
        from fastvideo.mlx_runtime.rife_interp import (
            interpolate_to_frame_count, load_model)
    except ImportError as exc:
        return _fail("rife_backend_missing",
                     f"读不到 fastvideo.mlx_runtime.rife_interp：{exc}")

    read_start = time.perf_counter()
    frames = [frame for frame in iio.imread(str(src))]
    read_s = time.perf_counter() - read_start
    if (args.expect_source_frames > 0
            and len(frames) != args.expect_source_frames):
        return _fail("frame_count_mismatch",
                     f"读出来是 {len(frames)} 帧，期望 {args.expect_source_frames} 帧；"
                     "时间轴或源文件变过，先别补帧")
    if len(frames) < 2:
        return _fail("too_few_frames", f"只有 {len(frames)} 帧，没什么可插的")

    # 目标帧数按 fps 换算，保证**时长不变**：补帧是把同一段时长填满更多帧，
    # 不是把片子拉长。round 而不是 ceil/floor：81 帧 16fps → 121.5 → 122，
    # 与 ffmpeg 自己按时长算出来的帧数一致（差一帧会让 post_check 的时长断言变抖）。
    target_frames = max(len(frames), round(len(frames) * args.target_fps / args.source_fps))
    if target_frames == len(frames):
        print("RESULT " + json.dumps({
            "ok": True, "skipped": "same_count",
            "source_frames": len(frames), "target_frames": target_frames,
        }, ensure_ascii=False))
        return 0

    load_start = time.perf_counter()
    model = load_model(version=args.model_version, weights_dir=args.weights_dir or None)
    load_s = time.perf_counter() - load_start

    interp_start = time.perf_counter()
    dense = interpolate_to_frame_count(frames, target_frames, model=model)
    interp_s = time.perf_counter() - interp_start
    if len(dense) != target_frames:
        return _fail("interp_count_wrong",
                     f"RIFE 给了 {len(dense)} 帧，要求 {target_frames} 帧")

    frames_dir.mkdir(parents=True, exist_ok=True)
    write_start = time.perf_counter()
    for index, frame in enumerate(dense):
        iio.imwrite(str(frames_dir / ("%05d.png" % index)), frame)
    write_s = time.perf_counter() - write_start

    print("RESULT " + json.dumps({
        "ok": True,
        "source_frames": len(frames),
        "target_frames": len(dense),
        "read_s": round(read_s, 3),
        "model_load_s": round(load_s, 3),
        "interp_s": round(interp_s, 3),
        "write_s": round(write_s, 3),
        "model_version": args.model_version,
    }, ensure_ascii=False))
    return 0


def _fail(code: str, message: str) -> int:
    """一行 JSON 出去，让调用方按 error_code 归一，不靠解析 stderr。"""
    print("RESULT " + json.dumps({"ok": False, "code": code, "message": message},
                                 ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
