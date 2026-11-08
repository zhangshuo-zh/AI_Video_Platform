"""导出前的一道 RIFE 补帧（2026-11-05 裁定＝开）。

要解决的问题是实测出来的，不是猜的：FastMetal 1.3B 的产物 `r_frame_rate=16/1`
（job 35 现测：81 帧、5.0625s），而成片按 24fps 出，`ffmpeg_runner` 只在末尾
挂了一个 `-r 24`——那是**复制帧**的 3:2 pulldown，动作会一顿一顿。这一道把
"复制帧"换成"按时间轴合成中间帧"。

三条设计决定，每条都有理由：

1. **不做静默兜底。** `COMPOSE_INTERPOLATE` 开着而运行时缺东西时，这条合成作业
   直接失败并点名缺什么，绝不退回"那就复制帧吧"。默默换做法会让同一条成片
   在不同机器上长成两种样子，是最难查的那类 bug。要退回旧行为请显式设
   `FASTVIDEO_COMPOSE_INTERPOLATE=0`。
2. **中间帧走 PNG 序列，不走中间 mp4。** 合成产物已经是一次 h264 有损编码，
   若补帧脚本再编一次 mp4、再让 ffmpeg 编第二次，画面就要过三代有损。
   PNG 无损，最后只编一次。
3. **音频从补帧前的文件里 copy 过来。** 补帧脚本只处理画面（imageio 读视频流），
   直接原地重写会把音轨丢掉——所以编码时用 `-map 1:a:0?` 把原文件的音频接上，
   不重新编码。

帧率与帧数一律 ffprobe 现读（`validators.probe_fps`），不信时间线里写的期望值：
时间线记的是"要 24fps"，而这一道关心的是"这个文件到底是多少"。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import config
import runtime
from config import (COMPOSE_CRF, COMPOSE_PRESET, FFMPEG_BIN, RIFE_MODEL_VERSION)
from runners import RunnerUnavailable

from composition import validators


class InterpolateUnavailable(Exception):
    """补帧这一道跑不了（缺运行时、缺权重、源帧率读不出）。不是画面坏了。"""


def plan(target_fps: float) -> dict:
    """这一次合成到底补不补帧，在**建命令的时候**就定死，不留到跑完再判。

    为什么必须在这里定：编码帧率取决于它。定了要补，ffmpeg 就按源帧率编，
    16→24 交给 RIFE；定了不补，就照旧直接编成目标帧率。要是等到补帧那一步
    才发现"其实不补"，文件已经按源帧率出了，成片就带着一个没人要的帧率。

    三档结论照 `runtime` 自己的词表来，不另造一套：
      run=False／disabled  显式关了（FASTVIDEO_COMPOSE_INTERPOLATE=0）
      run=False／runtime_unknown  没配 FASTVIDEO_FASTMETAL_HOME，平台查不了
      抛 InterpolateUnavailable      配了但东西不齐（missing）——**不许退回复制帧**

    "查不了"可以跳过并在回执里写明，"配了却没齐"必须响：前者是还没决定要用，
    后者是说了要用而东西不在。这两档在 `runtime.py` 的模块头就被分开，
    在这里也得保持分开，否则"unknown"和"missing"又会糊成一句"不可用"。
    """
    if not config.COMPOSE_INTERPOLATE:
        return {"run": False, "reason": "disabled",
                "detail": "补帧已显式关闭（FASTVIDEO_COMPOSE_INTERPOLATE=0），"
                          "按复制帧编码"}
    probe = runtime.probe_rife()
    if probe.state == "unknown":
        return {"run": False, "reason": "runtime_unknown", "detail": probe.detail}
    if probe.state != "ok":
        raise InterpolateUnavailable(
            "补帧这道开着，但运行时不齐：%s。要么补齐（FastVideo 仓库、"
            "其 .venv、RIFE 权重），要么显式设 FASTVIDEO_COMPOSE_INTERPOLATE=0 "
            "退回复制帧编码" % probe.detail)
    return {"run": True, "to_fps": float(target_fps), "detail": "RIFE 补帧"}


def enabled(source_fps: float | None, target_fps: float) -> bool:
    """动手前最后一道：源与目标本来就同帧率就什么都不做。

    为什么读 `config.COMPOSE_INTERPOLATE` 而不是 `from config import` 那个名字：
    开关要能在测试里翻。写成模块级常量绑死，`monkeypatch.setattr(config, ...)`
    就打不到这里——一个翻不动的开关，等于没人能证明它真的在管事情。
    """
    if not config.COMPOSE_INTERPOLATE:
        return False
    if not source_fps or source_fps <= 0:
        return False
    return abs(source_fps - target_fps) > 0.01


def interpolate(video: Path, outputs: Path, job_id: int, target_fps: float,
                log=None) -> dict:
    """原地把 `video` 补到 `target_fps`，返回可进 resources 的度量。

    失败一律抛 `InterpolateUnavailable`／`RunnerUnavailable`，由 worker 归一成
    作业失败——**不留半套产物**：临时文件与 PNG 序列在 finally 里清掉，
    原文件在成功之前一个字节都不动。
    """
    probe = runtime.probe_rife()
    if probe.state == "unknown":
        # 正常路径不会走到这儿——`plan()` 在建命令时就按同一句判过。
        # 留这一档是给"建完命令之后环境才被动过"的窗口，宁可跳过并写明，
        # 也不要拿着一个没配好的路径去起进程。
        return {"enabled": False, "skipped": "runtime_unknown", "detail": probe.detail}
    if probe.state != "ok":
        raise InterpolateUnavailable(
            "补帧运行时不齐：%s（要退回复制帧请显式设 FASTVIDEO_COMPOSE_INTERPOLATE=0）"
            % probe.detail)
    v = probe.values

    source_fps, source_frames = validators.probe_fps(video)
    if not source_fps:
        raise InterpolateUnavailable(
            f"读不出 {video.name} 的平均帧率，补帧不做")
    workdir = outputs / ("%s-interp" % job_id)
    frames_dir = workdir / "frames"
    pre = outputs / ("%s-pre-interp.mp4" % job_id)
    final = workdir / "out.mp4"
    os.replace(video, pre)                       # 先让开：原文件改名保留，成功再换回
    argv_pass = [v["python"], v["script"],
                 "--input", str(pre),
                 "--frames-dir", str(frames_dir),
                 "--source-fps", _fps_arg(source_fps),
                 "--target-fps", _fps_arg(target_fps),
                 "--expect-source-frames", str(source_frames or 0),
                 "--model-version", RIFE_MODEL_VERSION]
    if v.get("weights"):
        # 显式把本地快照目录递过去。不递的话 `load_model()` 会走
        # huggingface_hub 去问一次 revision——那等于"补帧要联网"，
        # 而这台机器是局域网内的生产机，出不出网是人决定的事，不是作业的副作用。
        # 顺带躲开一个真踩过的坑：本机 NO_PROXY 末尾挂着一个裸 `::1`，
        # httpx 建客户端时会把它当 URL 解析并抛 `Invalid port: ':1]'`，
        # 于是一条本来能跑的补帧作业直接失败。不走网络，这个雷就不在路径上。
        argv_pass += ["--weights-dir", str(v["weights"])]
    try:
        result = _run(argv_pass, "补帧", log)
        if result.get("skipped"):
            # 源与目标帧数算下来一样（例如 fps 只差一点点）：把原文件放回，不动。
            os.replace(pre, video)
            return {"enabled": True, "skipped": result["skipped"],
                    "source_fps": source_fps, "target_fps": target_fps,
                    "source_frames": result.get("source_frames"),
                    "target_frames": result.get("target_frames")}
        encode = _encode(frames_dir, pre, final, target_fps)
        _run_argv(encode, "补帧后编码", log)
        if not final.is_file() or final.stat().st_size <= 0:
            raise InterpolateUnavailable("补帧后编码没有产出文件")
        got_fps, got_frames = validators.probe_fps(final)
        if not got_fps or abs(got_fps - target_fps) > 0.5:
            raise InterpolateUnavailable(
                "补帧结果的实际帧率是 %s，要求 %s" % (got_fps, target_fps))
        os.replace(final, video)
        return {"enabled": True, "source_fps": source_fps, "target_fps": target_fps,
                "source_frames": source_frames, "target_frames": got_frames,
                "model_version": RIFE_MODEL_VERSION,
                "interp_s": result.get("interp_s"),
                "model_load_s": result.get("model_load_s"),
                "read_s": result.get("read_s"), "write_s": result.get("write_s")}
    finally:
        # 收尾的第一件事是**把原片放回去**（2026-11-08，P0 丢产物）。
        #
        # 进这一段时 `video` 已经被改名成 `pre`（上面那句 os.replace），
        # 只有三条成功路径会把它换回来：skipped、正常换 `final`、以及…没有了。
        # 补帧脚本报错、编码失败、帧率不符、"补帧后编码没有产出文件"——
        # 这些异常都会走到 finally，而原来的 finally 只做 rmtree + `pre.unlink()`：
        # 那个**已经合成成功、只是等着补帧**的成片于是被直接删掉，
        # 作业记 failed，人只能整条合成重跑一遍。
        # `config.py` 那句"这一道会直接失败，不会退回复制帧"是说自己不悄悄降级，
        # 不是说可以连原片一起赔进去。
        #
        # 判据用"video 不在而 pre 在"：成功路径已经把 video 换回来了，
        # 这时 pre 也不存在，不会误动。
        if not video.exists() and pre.is_file():
            try:
                os.replace(pre, video)
            except OSError:
                # 放不回去就**不要**删 pre（下面那句 unlink 靠 video.exists() 把关）：
                # 它至少是那条成片的唯一副本，删掉就真的没了。
                pass
        shutil.rmtree(workdir, ignore_errors=True)
        # 只有在成片确实回到 video 上时才清 pre：movie 已经在了，pre 就是多余的备份；
        # 成片没回来（上面 replace 失败）时留着它，让人有机会手工捞回来。
        if video.exists() and pre.is_file():
            try:
                pre.unlink()
            except OSError:
                pass


def _encode(frames_dir: Path, audio_source: Path, out: Path, target_fps: float) -> list[str]:
    ffmpeg = shutil.which(FFMPEG_BIN)
    if not ffmpeg:
        raise InterpolateUnavailable(f"找不到 ffmpeg（{FFMPEG_BIN}）")
    return [ffmpeg, "-hide_banner", "-y",
            "-framerate", _fps_arg(target_fps), "-i", str(frames_dir / "%05d.png"),
            "-i", str(audio_source),
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "libx264", "-preset", COMPOSE_PRESET, "-crf", str(COMPOSE_CRF),
            "-pix_fmt", "yuv420p", "-r", _fps_arg(target_fps),
            "-c:a", "copy", "-movflags", "+faststart", str(out)]


def _run(argv: list[str], label: str, log=None) -> dict:
    """跑补帧脚本，把它最后一行 `RESULT {...}` 解析出来。"""
    out = _run_argv(argv, label, log)
    for line in reversed((out or "").splitlines()):
        if line.startswith("RESULT "):
            try:
                payload = json.loads(line[len("RESULT "):])
            except ValueError:
                raise InterpolateUnavailable("%s 的回执读不出来" % label)
            if not payload.get("ok"):
                raise InterpolateUnavailable(
                    "%s 失败（%s）：%s" % (label, payload.get("code"), payload.get("message")))
            return payload
    raise InterpolateUnavailable("%s 没有给出回执" % label)


def _run_argv(argv: list[str], label: str, log=None) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800,
                              shell=False)
    except FileNotFoundError as exc:
        raise InterpolateUnavailable("%s 起不来：%s" % (label, exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise InterpolateUnavailable("%s 超时（1800 秒）" % label) from exc
    if log is not None:
        try:
            log.write("[%s] exit=%s %s\n" % (label, proc.returncode,
                                             (proc.stderr or "").strip()[-400:]))
            log.flush()
        except (OSError, ValueError):
            pass
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["（无输出）"]
        raise RunnerUnavailable("%s 退出码 %s：%s" % (label, proc.returncode, tail[0][:200]))
    return proc.stdout or ""


def _fps_arg(value: float) -> str:
    """24.0 → "24"，23.976 → "23.976"。ffmpeg 收两种，但库里的口径统一成最短形式。"""
    text = ("%.6f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"
