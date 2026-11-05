#!/usr/bin/env python3
"""导出前那道 RIFE 补帧的真取证：拿一段真 16fps 片子补到 24fps，逐量实测。

为什么要有这个工具，而不是"跑一条合成作业看看"：合成作业要项目、时间线、
镜头产物一整套业务前提，出问题时分不清是补帧错了还是链路别的环节错了。
这里只把这一道单独拿出来，用 ffmpeg 现造一段**已知帧率与帧数**的素材，
断言的三件事都是可计算的：平均帧率、总帧数、音轨还在不在。

三条纪律：
1. **只走平台的真代码**（`composition.interpolate.interpolate`），
   不在这里重抄一遍 argv——那等于证明了我自己写的脚本，而不是证明线上那条路。
2. **测音频是必须的**：补帧脚本只处理画面，音频是从补帧前的文件 copy 回来的。
   少了这一项断言，"补完帧变成默片"这种事故会在成片里才被发现。
3. 素材是 ffmpeg 造的，不是模型出的——这条只证明补帧这道对**任何** 16fps 输入成立，
   不代替 FastMetal 真产物的验收（那要跑一条真合成作业，见《黄金样例与故障演练》）。

用法（用后端那个 venv 的 python，它才 import 得到 fastapi 之外的兄弟模块）：
    FASTVIDEO_FASTMETAL_HOME=$HOME/FastVideo \\
      app/backend/.venv/bin/python tools/rife_interp_check.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "app" / "backend"
sys.path.insert(0, str(HERE))

import runtime                                    # noqa: E402
from composition import interpolate                # noqa: E402
from composition.validators import probe_fps       # noqa: E402

WORK = Path("/tmp/rife-check")
SOURCE_FPS = 16.0
TARGET_FPS = 24.0
SECONDS = 2


def sh(argv: list[str], label: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True, shell=False, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"{label} 失败（退出码 {proc.returncode}）："
                         f"{(proc.stderr or '').strip()[-300:]}")
    return proc


def make_source(path: Path) -> None:
    """现造一段 16fps、带 440Hz 音轨的小片子——帧率与时长都是我们自己定的已知量。"""
    sh([_bin("ffmpeg"), "-hide_banner", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=320x192:rate={int(SOURCE_FPS)}",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(SECONDS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-pix_fmt", "yuv420p", str(path)], "造素材")


def _bin(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"找不到 {name}；这道检查需要 FFmpeg")
    return found


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    outputs = WORK / "outputs"
    outputs.mkdir()

    probe = runtime.probe_rife()
    print("补帧运行时：%-8s %s" % (probe.state, probe.detail))
    for key in ("python", "backend", "weights", "script"):
        if probe.values.get(key):
            print("   %-8s %s" % (key, probe.values[key]))
    if probe.state != "ok":
        print("\n运行时不齐，这道检查跑不了。缺什么照上面那句补，"
              "或者显式设 FASTVIDEO_COMPOSE_INTERPOLATE=0 承认这台机器不做补帧。")
        return 2

    src = outputs / "999999-compose.mp4"
    make_source(src)
    before_fps, before_frames = probe_fps(src)
    print("\n补帧前：%.3f fps、%s 帧、%d 字节" % (
        before_fps, before_frames, src.stat().st_size))

    started = time.perf_counter()
    metrics = interpolate.interpolate(src, outputs, 999999, TARGET_FPS)
    wall = time.perf_counter() - started
    after_fps, after_frames = probe_fps(src)

    expect_frames = round(before_frames * TARGET_FPS / before_fps)
    checks = [
        ("平均帧率补到目标", after_fps is not None
         and abs(after_fps - TARGET_FPS) <= 0.5, "%.3f" % (after_fps or -1)),
        ("帧数按时长换算", after_frames == expect_frames,
         "%s（期望 %s）" % (after_frames, expect_frames)),
        ("时长没被拉长", abs((after_frames or 0) / TARGET_FPS
                    - (before_frames or 0) / before_fps) < 0.12,
         "%.2fs → %.2fs" % ((before_frames or 0) / before_fps,
                            (after_frames or 0) / TARGET_FPS)),
        ("音频还在（补帧只动画面）", _has_audio(src), str(_has_audio(src))),
        ("临时文件与二代编码已清理", not list(outputs.glob("*pre-interp*"))
         and not (outputs / "999999-interp").exists(),
         "补帧前的原件与 PNG 序列都不该留下（留一份就等于多占一份盘）"),
    ]
    print("补帧后：%.3f fps、%s 帧、%d 字节；墙钟 %.1fs" % (
        after_fps or -1, after_frames, src.stat().st_size, wall))
    print("分段耗时：" + json.dumps({
        "read": metrics.get("read_s"), "model_load": metrics.get("model_load_s"),
        "interp": metrics.get("interp_s"), "write_png": metrics.get("write_s")},
        ensure_ascii=False))

    failed = 0
    for name, ok, detail in checks:
        print("  [%s] %s：%s" % ("绿" if ok else "红", name, detail))
        if not ok:
            failed += 1
    print("\n%d 项通过 / %d 项" % (len(checks) - failed, len(checks)))
    return 0 if not failed else 1


def _has_audio(path: Path) -> bool:
    proc = subprocess.run([_bin("ffprobe"), "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=codec_name", "-of", "json", str(path)],
                          capture_output=True, text=True, shell=False, timeout=60)
    try:
        return bool((json.loads(proc.stdout or "{}")).get("streams"))
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
