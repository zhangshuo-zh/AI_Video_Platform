"""导出前那道 RIFE 补帧的守卫（2026-11-05 裁定＝合成后、导出前补帧到目标帧率）。

这些用例守的是三件事，每件都对应一个已经踩到或即将踩到的坑：

1. **不许静默退回复制帧。** `plan()` 在"配了但东西不齐"时必须抛，不能返回
   `run=False`——静默退让会让同一份成片在不同机器上长成两种样子。
   而"还没配"（unknown）是另一档，跳过并把原因写进回执，这条与
   `runtime.py` 模块头把 unknown 与 missing 分开的口径一致。
2. **旧行为要逐字不变。** 没排到补帧时，合成命令里的 `-r` 必须还是目标帧率；
   没显式给 `fps` 时，FastMetal 的 argv 里不该凭空多出 `--fps`。
   否则"历史作业可复现"这条就是空话。
3. **帧率这道闸门能变红。** `post_check` 以前不查帧率，所以"声明 24、
   实际是复制帧"一直过得去。这里正反各测一次：对得上放行、对不上拦下。

真链路（真 RIFE、真重编码、音频回接）不在这里跑——那是
`tools/rife_interp_check.py` 的活，它实测过 16fps/32 帧 → 24fps/48 帧。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import adapters
import config
import runtime
import runners
from composition import ffmpeg_runner, interpolate, validators
from config import FFMPEG_BIN


def _fake_rife_home(root: Path) -> Path:
    """造一个够真的 FastVideo 仓库：解释器、后端模块、权重目录。"""
    home = root / "FastVideo"
    python = home / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    backend = home / "fastvideo" / "mlx_runtime" / "rife_interp.py"
    backend.parent.mkdir(parents=True)
    backend.write_text("# fake\n")
    weights = root / "RIFE-4.25"
    weights.mkdir()
    (weights / "config.json").write_text("{}")
    (weights / "model.safetensors").write_text("fake")
    return home


# ---------- runtime.probe_rife ----------

def test_补帧运行时没配是unknown而不是missing(monkeypatch):
    monkeypatch.delenv("FASTVIDEO_FASTMETAL_HOME", raising=False)
    checked = runtime.probe_rife()
    assert checked.state == "unknown"
    assert "FASTVIDEO_FASTMETAL_HOME" in checked.detail


def test_配了但权重不在是missing并点名缺什么(tmp_path, monkeypatch):
    home = _fake_rife_home(tmp_path)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    monkeypatch.setenv("FASTVIDEO_RIFE_WEIGHTS", str(tmp_path / "没有这个目录"))
    checked = runtime.probe_rife()
    assert checked.state == "missing"
    assert "RIFE" in checked.detail


def test_四件套齐了才说ok(tmp_path, monkeypatch):
    home = _fake_rife_home(tmp_path)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    monkeypatch.setenv("FASTVIDEO_RIFE_WEIGHTS", str(tmp_path / "RIFE-4.25"))
    checked = runtime.probe_rife()
    assert checked.state == "ok", checked.detail
    assert checked.values["weights"].endswith("RIFE-4.25")


# ---------- plan(): 决定补不补，在建命令时就定死 ----------

def test_显式关掉就不补并写明原因(monkeypatch):
    monkeypatch.setattr(config, "COMPOSE_INTERPOLATE", False)
    plan = interpolate.plan(24.0)
    assert plan["run"] is False and plan["reason"] == "disabled"


def test_没配运行时跳过但不失败(monkeypatch):
    monkeypatch.setattr(config, "COMPOSE_INTERPOLATE", True)
    monkeypatch.setattr(runtime, "probe_rife",
                        lambda: runtime.Runtime("unknown", "没配", {}))
    plan = interpolate.plan(24.0)
    assert plan["run"] is False and plan["reason"] == "runtime_unknown"


def test_配了却没齐必须抛不许退回复制帧(monkeypatch):
    monkeypatch.setattr(config, "COMPOSE_INTERPOLATE", True)
    monkeypatch.setattr(runtime, "probe_rife",
                        lambda: runtime.Runtime("missing", "缺权重", {}))
    with pytest.raises(interpolate.InterpolateUnavailable) as exc:
        interpolate.plan(24.0)
    assert "FASTVIDEO_COMPOSE_INTERPOLATE=0" in str(exc.value)
    assert "退回" in str(exc.value)


def test_帧率格式化不写出无意义的24_0():
    assert interpolate._fps_arg(24.0) == "24"
    assert interpolate._fps_arg(23.976) == "23.976"


# ---------- build(): 补帧与否决定编码帧率，旧路径逐字不变 ----------

def _timeline(tmp_path: Path, shots: list[tuple[int, str]]) -> dict:
    return {"project_id": 1, "total_duration_ms": 2000,
            "shots": [{"seq": seq, "duration_ms": 1000,
                       "video": {"path": rel}} for seq, rel in shots],
            "audio": [], "subtitles": []}


def _stub_build_env(monkeypatch, tmp_path: Path, rates: list[float | None]):
    """把 build() 需要的库与文件都换成假的，只留"帧率怎么定"这一件事。"""
    outputs = tmp_path / "outputs"
    outputs.mkdir(exist_ok=True)
    files = []
    for index in range(len(rates)):
        fake = tmp_path / f"shot{index}.mp4"
        fake.write_text("x")
        files.append(f"jobs/{index}/outputs/shot{index}.mp4")

    monkeypatch.setattr(ffmpeg_runner, "resolve_artifact",
                        lambda rel: tmp_path / f"shot{rel.split('/')[1]}.mp4")
    monkeypatch.setattr(validators, "verify_inputs", lambda timeline: [])
    row = {"project_id": 1, "timeline_json": json.dumps(
               _timeline(tmp_path, list(zip(range(1, len(rates) + 1), files)))),
           "params_json": "{}"}
    import repositories.compositions as repo_module
    monkeypatch.setattr(repo_module.compositions, "get", lambda cid: row)
    cycle = iter(rates)
    monkeypatch.setattr(validators, "probe_fps", lambda path: (next(cycle), 24))
    job = {"id": 4242, "project_id": 1, "capability": "video",
           "provider_code": "ffmpeg", "model_code": ""}
    return job, outputs


def test_不补帧时编码帧率仍是目标值(tmp_path, monkeypatch):
    monkeypatch.setattr(interpolate, "plan",
                        lambda fps: {"run": False, "reason": "disabled"})
    job, outputs = _stub_build_env(monkeypatch, tmp_path, [16.0])
    spec = ffmpeg_runner.build(job, {"width": 640, "height": 360, "fps": 24,
                                     "composition_id": 7}, outputs)
    assert "-r" in spec.argv and spec.argv[spec.argv.index("-r") + 1] == "24.0"
    assert spec.meta["interp"] is None


def test_要补帧时按源帧率编码并把目标交给worker(tmp_path, monkeypatch):
    monkeypatch.setattr(interpolate, "plan", lambda fps: {"run": True})
    job, outputs = _stub_build_env(monkeypatch, tmp_path, [16.0])
    spec = ffmpeg_runner.build(job, {"width": 640, "height": 360, "fps": 24,
                                     "composition_id": 7}, outputs)
    assert spec.argv[spec.argv.index("-r") + 1] == "16.0"
    assert spec.meta["interp"] == {"from_fps": 16.0, "to_fps": 24.0, "shots": 1}
    # 期望里写的仍是**最终**帧率：post_check 查的是补完帧那个文件
    assert spec.meta["expected"]["fps"] == 24


def test_镜头帧率不一致就停下点名(tmp_path, monkeypatch):
    monkeypatch.setattr(interpolate, "plan", lambda fps: {"run": True})
    job, outputs = _stub_build_env(monkeypatch, tmp_path, [16.0, 24.0])
    with pytest.raises(interpolate.InterpolateUnavailable) as exc:
        ffmpeg_runner.build(job, {"width": 640, "height": 360, "fps": 24,
                                  "composition_id": 7}, outputs)
    assert "16.0" in str(exc.value) and "24.0" in str(exc.value)


# ---------- post_check 的帧率闸门 ----------

def _clip_at(tmp_path: Path, fps: int) -> Path:
    path = tmp_path / f"clip{fps}.mp4"
    subprocess.run([FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
                    "-i", f"color=c=red:s=128x96:d=1", "-r", str(fps),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   capture_output=True, check=True)
    return path


def test_probe_fps读得出真实帧率(tmp_path):
    path = _clip_at(tmp_path, 16)
    fps, frames = validators.probe_fps(path)
    assert abs(fps - 16.0) < 0.01
    assert frames and frames >= 15


def _spec_for_check(path: Path, want_fps: int) -> runners.RunSpec:
    return runners.RunSpec(
        runner="ffmpeg", argv=[], cwd=path.parent, expected=[path], media="video",
        meta={"expected": {"width": 128, "height": 96, "total_duration_ms": 1000,
                           "fps": want_fps, "audio": False},
              "compose_json": "", "interp": None})


def test_帧率对得上放行(tmp_path):
    info = ffmpeg_runner.post_check(_spec_for_check(_clip_at(tmp_path, 24), 24))
    assert abs(info["avg_fps"] - 24.0) < 0.01


def test_声明24实际16要拦下(tmp_path):
    """这条就是"补帧没生效却报成功"的兜底——以前这里完全不查帧率。"""
    with pytest.raises(validators.MediaInvalid) as exc:
        ffmpeg_runner.post_check(_spec_for_check(_clip_at(tmp_path, 16), 24))
    assert "平均帧率" in exc.value.message


# ---------- FastMetal 的三个新参数 ----------

def _fake_fastmetal(root: Path) -> Path:
    home = root / "FastVideo"
    python = home / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    script = home / "examples" / "inference" / "basic" / "mlx_wan_prompt_to_video.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fake\n")
    model = home / "FastMetal-1.3B-QAD"
    model.mkdir(parents=True)
    for item in ("mlx_dit.json", "mlx_dit.safetensors"):
        (model / item).write_text("{}")
    for item in ("tokenizer", "text_encoder", "vae"):
        (model / item).mkdir(parents=True)
    return home


def _job(**overrides):
    base = {"id": 7, "prompt_snapshot": "一支火把", "capability": "video",
            "execution_mode": "local", "provider_code": "fastmetal",
            "model_code": "fastmetal-1.3b-qad"}
    base.update(overrides)
    return base


def test_不给fps时argv一个字节都不变(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(_fake_fastmetal(tmp_path)))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    spec = runners.build(_job(), {}, outputs)
    assert "--fps" not in spec.argv
    assert "--fast-factor" not in spec.argv
    assert "--fast-sharpen" not in spec.argv


def test_显式给了才下发(tmp_path, monkeypatch):
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(_fake_fastmetal(tmp_path)))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    spec = runners.build(_job(), {"fps": 24, "fast": True, "fast_factor": 2,
                                  "fast_sharpen": 0.5}, outputs)
    assert spec.argv[spec.argv.index("--fps") + 1] == "24"
    assert spec.argv[spec.argv.index("--fast-factor") + 1] == "2"
    assert spec.argv[spec.argv.index("--fast-sharpen") + 1] == "0.5"


def test_三个新参数的范围在清单里核过():
    adapter = adapters.find("video", "local", "fastmetal")
    assert adapters.validate_params(adapter, "fastmetal-1.3b-qad",
                                    {"fps": 24, "fast_factor": 2}) == []
    errors = adapters.validate_params(adapter, "fastmetal-1.3b-qad", {"fps": 200})
    assert errors and "60" in errors[0]
    assert adapters.validate_params(adapter, "fastmetal-1.3b-qad",
                                    {"fast_factor": 4}) == [], "4 在范围内应当放行"
    assert adapters.validate_params(adapter, "fastmetal-1.3b-qad", {"fast_factor": 9})
