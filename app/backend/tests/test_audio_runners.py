"""配音与音乐两条执行分支（VoxCPM2／ACE-Step 1.5，都经 mlx-audio）。

这四件事必须有用例钉着，因为它们各自对应一种"看起来成功"的假象：

1. **能力表不许再说反话**。`production._audio_wired()` 以前把 `runner_wired`
   写死成 False；接线之后如果还写死，界面就会指着一条已经能跑的路说"没接线"。
   所以这里判的是 `runners.has_branch()` 与 `build()` 的**一致性**，
   而不是任何一个具体布尔值。
2. **没有运行时就说没有**。子进程找不到入口时必须 RunnerUnavailable 且**不落产物**，
   不能像 mock 那样造一个假 wav。
3. **音频产物要验真**。一段被截断的字节流、一张 HTML 错误页，都不算音频。
4. **TTS 的分段文件名由执行器决定**，所以产物靠 `expected_glob` 事后找；
   找不到任何文件要抛，不写半条成功。
"""
from pathlib import Path

import pytest
import runners
from runners import RunSpec


def _job(capability, provider, model, text="测试文本", **params):
    return {"id": 777, "capability": capability, "execution_mode": "local",
            "provider_code": provider, "model_code": model,
            "prompt_snapshot": text, "params": params}


# ---------------------------------------------------------------- 分支一致性

def test_has_branch_与_build_说的是同一件事():
    """写死的布尔与真实的分支必须一起变；这条用例防的就是"表说没接线"。"""
    for capability, provider, wired in (
            ("tts", "voxcpm2", True), ("tts", "voxcpm", True),
            ("music", "ace-step", True), ("music", "acestep", True),
            ("tts", "volc", False), ("music", "suno", False),
            ("soundfx", "ace-step", False), ("tts", "mflux", False)):
        assert runners.has_branch(capability, provider) is wired, (capability, provider)
        if not wired and provider in ("volc", "suno", "mflux"):
            with pytest.raises(runners.RunnerUnavailable):
                runners.build(_job(capability, provider, "x"), {}, Path("/tmp"))


def test_能力表跟着代码走(monkeypatch, tmp_path):
    """`_audio_wired()` 的 runner_wired 来自 has_branch，不是常数。"""
    import production
    monkeypatch.setattr(runners, "has_branch", lambda c, p: True)
    view = production.audio_status_view()
    rows = {r["capability"]: r for r in view["rows"]}
    assert rows["tts"]["runner_wired"] is True
    assert rows["music"]["runner_wired"] is True


# ---------------------------------------------------------------- 两条 argv

def test_配音命令行带模型文本与输出目录(monkeypatch, tmp_path):
    monkeypatch.setattr(runners.runtime, "probe_mlx_audio",
                        lambda which="tts": runners.runtime._ok(
                            "ok", cli="/usr/local/bin/mlx_audio.tts.generate"))
    spec = runners.build(_job("tts", "voxcpm2", "voxcpm2", "你迟了三年。"),
                         {"speed": 1.2}, tmp_path)
    assert spec.argv[0] == "/usr/local/bin/mlx_audio.tts.generate"
    assert "--model" in spec.argv and "mlx-community/VoxCPM2-8bit" in spec.argv
    assert spec.argv[spec.argv.index("--text") + 1] == "你迟了三年。"
    assert spec.argv[spec.argv.index("--speed") + 1] == "1.2"
    assert spec.media == "audio"
    # 分段名由执行器决定 ⇒ 预期产物只能事后按模式找
    assert spec.expected == [] and spec.expected_glob.startswith("777")


def test_配音没有文本时直接拒(monkeypatch, tmp_path):
    monkeypatch.setattr(runners.runtime, "probe_mlx_audio",
                        lambda which="tts": runners.runtime._ok("ok", cli="/x"))
    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job("tts", "voxcpm2", "voxcpm2", "   "), {}, tmp_path)


def test_音乐命令行带歌词兜底与产物路径(monkeypatch, tmp_path):
    monkeypatch.setattr(runners.runtime, "probe_mlx_audio",
                        lambda which="music": runners.runtime._ok(
                            "ok", cli="/usr/local/bin/mlx_audio.music.generate"))
    spec = runners.build(_job("music", "ace-step", "ace-step-1.5",
                              "夜间灯市，弦乐与鼓"),
                         {"duration": 45, "steps": 20, "seed": 7}, tmp_path)
    assert spec.argv[spec.argv.index("--caption") + 1] == "夜间灯市，弦乐与鼓"
    assert spec.argv[spec.argv.index("--duration") + 1] == "45"
    assert spec.argv[spec.argv.index("--steps") + 1] == "20"
    assert spec.argv[spec.argv.index("--seed") + 1] == "7"
    # CLI 的 --lyrics 是必填互斥组：没给歌词时要兜成结构标记，不能留空串
    assert spec.argv[spec.argv.index("--lyrics") + 1] == runners.ACE_STEP_INSTRUMENTAL
    assert spec.expected == [tmp_path / "777-music.wav"]


def test_找不到执行入口就拒且不造产物(monkeypatch, tmp_path):
    monkeypatch.setattr(runners.runtime, "probe_mlx_audio",
                        lambda which="tts": runners.runtime._missing(
                            "找不到 mlx_audio.tts.generate"))
    with pytest.raises(runners.RunnerUnavailable) as exc:
        runners.build(_job("tts", "voxcpm2", "voxcpm2"), {}, tmp_path)
    assert "mlx_audio" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- 验真与发现

def test_音频验真认_wav_不认_html(tmp_path):
    good = tmp_path / "good.wav"
    good.write_bytes(b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"data" + b"\x00" * 8)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"<html><body>502 Bad Gateway</body></html>")
    assert runners._magic_ok(good, "audio") is True
    assert runners._magic_ok(bad, "audio") is False
    spec = RunSpec(runner="voxcpm2", argv=[], cwd=tmp_path,
                   expected=[bad], media="audio")
    with pytest.raises(runners.RunnerUnavailable):
        runners.verify(spec)


def test_按模式找产物_找不到就抛(tmp_path):
    out = tmp_path / "777-tts"
    out.mkdir()
    spec = RunSpec(runner="voxcpm2", argv=[], cwd=out, expected=[], media="audio",
                   expected_glob="777*.wav")
    with pytest.raises(runners.RunnerUnavailable):
        runners.verify(spec)
    (out / "777_001.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    (out / "777_000.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    paths = runners.verify(spec)
    assert [p.name for p in paths] == ["777_000.wav", "777_001.wav"]
