"""W09 的命令构造与运行时探测。

这几条测试**不跑模型**：它们证明 argv 是数组、路径从运行时探测来、
缺东西时在起进程之前就明确失败。真正的 mp4 由 `盘点/W09_BASELINE.md`
记录的验收作业负责，不塞进 pytest。
"""
import json
from pathlib import Path

import pytest

import adapters
import config
import runners
import runtime


def _fake_fastmetal(root: Path, name: str = "FastMetal-1.3B-QAD") -> Path:
    home = root / "FastVideo"
    python = home / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    script = home / "examples" / "inference" / "basic" / "mlx_wan_prompt_to_video.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fake\n")
    model = home / name
    model.mkdir(parents=True)
    for item in ("mlx_dit.json", "mlx_dit.safetensors"):
        (model / item).write_text("{}")
    for item in ("tokenizer", "text_encoder", "vae"):
        (model / item).mkdir(parents=True)
    return home


def _job(**overrides):
    base = {
        "id": 7,
        "prompt_snapshot": '烟花 $HOME; rm -rf / -- "引号" $(echo x)',
        "capability": "video",
        "execution_mode": "local",
        "provider_code": "fastmetal",
        "model_code": "fastmetal-1.3b-qad",
    }
    base.update(overrides)
    return base


def test_fastmetal_命令是数组且提示词原样传递(tmp_path, monkeypatch):
    home = _fake_fastmetal(tmp_path)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    monkeypatch.delenv("FASTVIDEO_FASTMETAL_MODEL", raising=False)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    spec = runners.build(_job(), {"width": 832, "height": 480, "frames": 81,
                                  "seed": 12, "fast": True, "fast_spatial": True},
                         outputs)

    assert isinstance(spec.argv, list) and all(isinstance(x, str) for x in spec.argv)
    assert spec.argv[0] == str(home / ".venv/bin/python")
    assert spec.argv[1].endswith("mlx_wan_prompt_to_video.py")
    # 提示词是一个独立 argv 元素：分号、引号、$() 都不会被 shell 解释
    assert spec.argv[spec.argv.index("--prompt") + 1] == _job()["prompt_snapshot"]
    assert spec.argv[spec.argv.index("--width") + 1] == "832"
    assert spec.argv[spec.argv.index("--num-frames") + 1] == "81"
    assert "--fast" in spec.argv
    assert "--fast-spatial" in spec.argv
    assert str(spec.expected[0]).endswith("7-fastmetal.mp4")
    assert spec.cwd == outputs
    # 清单不再摆假的步数旋钮：DMD 固定 3 步
    assert "--num-inference-steps" not in spec.argv


def test_fastmetal_缺权重时在起进程前失败(tmp_path, monkeypatch):
    home = _fake_fastmetal(tmp_path)
    (home / "FastMetal-1.3B-QAD" / "vae" / "config.json").unlink(missing_ok=True)
    (home / "FastMetal-1.3B-QAD" / "vae").rmdir()
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    with pytest.raises(runners.RunnerUnavailable) as exc:
        runners.build(_job(), {}, outputs)
    assert "vae" in exc.value.message


def test_运行时探测与健康检查同一口径(tmp_path, monkeypatch):
    home = _fake_fastmetal(tmp_path)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    checked = runtime.probe_fastmetal("fastmetal-1.3b-qad")
    assert checked.state == "ok"
    row = {"provider": "fastmetal", "capability": "video", "backend": "local",
           "model": "fastmetal-1.3b-qad", "key_env": ""}
    health = adapters.health(row)
    assert health["state"] == "ok"
    assert "权重" in health["detail"]


def test_选了provider但没填模型档先说清楚(tmp_path, monkeypatch):
    home = _fake_fastmetal(tmp_path)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    row = {"provider": "fastmetal", "capability": "video", "backend": "local",
           "model": "", "key_env": ""}
    health = adapters.health(row)
    assert health["state"] == "missing"
    assert "模型" in health["detail"]


def test_缺运行时是一个可读的missing而不是unknown(monkeypatch):
    monkeypatch.delenv("FASTVIDEO_FASTMETAL_HOME", raising=False)
    checked = runtime.probe_fastmetal("fastmetal-1.3b-qad")
    assert checked.state == "unknown"
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", "/nonexistent/w09")
    checked = runtime.probe_fastmetal("fastmetal-1.3b-qad")
    assert checked.state == "missing"
    assert "不存在" in checked.detail


def test_mflux_只认_cli_不猜权重(tmp_path, monkeypatch):
    cli = tmp_path / "mflux-generate"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setenv("FASTVIDEO_MFLUX_BIN", str(cli))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    spec = runners.build(_job(capability="image", provider_code="mflux",
                              model_code="flux1-schnell"),
                         {"width": 512, "height": 512, "steps": 4, "seed": 9},
                         outputs)
    assert spec.argv[0] == str(cli)
    assert spec.argv[spec.argv.index("--model") + 1] == "schnell"
    assert spec.argv[spec.argv.index("--output") + 1].endswith(".png")
    assert spec.media == "image"


def test_mlx_video_条件图只能落在作业目录里(tmp_path, monkeypatch):
    home = tmp_path / "mlx-video"
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (home / ".venv" / "bin" / "python").chmod(0o755)
    (home / "mlx_video").mkdir()
    model = tmp_path / "wan22-i2v"
    model.mkdir()
    (model / "config.json").write_text("{}")
    monkeypatch.setenv("FASTVIDEO_MLX_VIDEO_HOME", str(home))
    monkeypatch.setenv("FASTVIDEO_MLX_VIDEO_MODEL", str(model))

    job_dir = config.JOBS_DIR / "7" / "inputs"
    job_dir.mkdir(parents=True, exist_ok=True)
    image = job_dir / "start.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    spec = runners.build(_job(provider_code="mlx-video",
                              model_code="wan2.2-i2v-a14b"),
                         {"image": "inputs/start.png", "frames": 81, "steps": 4,
                          "width": 1280, "height": 704}, outputs)
    assert "-m" in spec.argv and "mlx_video.wan_2.generate" in spec.argv
    assert spec.argv[spec.argv.index("--image") + 1] == str(image.resolve())

    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job(provider_code="mlx-video",
                           model_code="wan2.2-i2v-a14b"),
                      {"image": "../../outside.png"}, outputs)


def test_产物必须验魔数与非空(tmp_path):
    spec = runners.RunSpec(runner="fastmetal", argv=["x"],
                           cwd=tmp_path, expected=[tmp_path / "a.mp4"],
                           media="video")
    (tmp_path / "a.mp4").write_bytes(b"not a video")
    with pytest.raises(runners.RunnerUnavailable):
        runners.verify(spec)
    (tmp_path / "a.mp4").write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8)
    assert runners.verify(spec) == spec.expected

    empty = runners.RunSpec(runner="fastmetal", argv=["x"],
                            cwd=tmp_path, expected=[tmp_path / "b.mp4"],
                            media="video")
    (tmp_path / "b.mp4").write_bytes(b"")
    with pytest.raises(runners.RunnerUnavailable):
        runners.verify(empty)


def test_metrics_只挑摘要不搬整份(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({
        "prompt": "x", "total_s": 12.5, "prompt_encode_s": 3.1,
        "mlx_denoise_peak_bytes": 2 * 1024 ** 3,
        "model_root": "/secret/path", "prompt_user": "x",
    }), encoding="utf-8")
    got = runners.metrics_summary(path)
    assert got["total_s"] == 12.5
    assert got["denoise_peak_gb"] == 2.0
    assert "model_root" not in got
