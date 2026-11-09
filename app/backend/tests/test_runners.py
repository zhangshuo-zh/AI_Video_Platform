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
        # `_is_platform_artifact` 判归属时要用（2026-11-09 外部审查第 2 条）。
        # 真实路径上 job 一律来自 `job_repo.get()`，是完整库行，这个字段必然在；
        # 夹具过去只造了边界判定用得到的那几列。
        "project_id": 1,
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


def test_fastmetal_空参数也照清单默认开fast_spatial(tmp_path, monkeypatch):
    """产线节点提交的作业 params 是空的，默认值必须从清单读回来。

    这是本轮真跑六场景时查出来的一条：清单给 1.3B 标 `fast_spatial=True`
    的依据是 W09 在 16GB 上的实测（640×352×81 不开就解码 OOM），
    但 runner 过去只看 `params.get(...)`——空参数就等于把这条实测丢掉，
    作业会以"模型跑崩了"的样貌失败。规格住代码，也要**真的被读到**才算数。
    """
    home = _fake_fastmetal(tmp_path)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    monkeypatch.delenv("FASTVIDEO_FASTMETAL_MODEL", raising=False)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    spec = runners.build(_job(), {}, outputs)

    assert "--fast-spatial" in spec.argv, spec.argv
    # 显式给 false 时不硬塞：清单默认是建议，不是命令
    off = runners.build(_job(), {"fast_spatial": False}, outputs)
    assert "--fast-spatial" not in off.argv


def test_fastmetal_缺权重时在起进程前失败(tmp_path, monkeypatch, delete_guard):
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
    # 走社区 Q4 转换档：官方仓库 gated 且整包 33.7GB，16GB 机器装不下 q8。
    assert spec.argv[spec.argv.index("--model") + 1] == \
        "mflux-community/flux-1-schnell-mflux-q4"
    assert spec.argv[spec.argv.index("--base-model") + 1] == "schnell"
    # 量化级别已写进权重；再传 --quantize 会与已存的量化打架
    assert "--quantize" not in spec.argv
    assert spec.argv[spec.argv.index("--output") + 1].endswith(".png")
    assert spec.media == "image"


def _fake_mlx_home(root: Path, monkeypatch) -> Path:
    """搭一个能过 `runtime.probe_mlx_video` 的假运行时：解释器 + 包目录 + 模型 config。

    只喂探测要的那几样，不起进程、不加载权重——这一份测试钉的是**边界**，
    不是模型能不能跑（真产物由 W09 的验收作业负责）。
    """
    home = root / "mlx-video"
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (home / ".venv" / "bin" / "python").chmod(0o755)
    (home / "mlx_video").mkdir()
    model = root / "wan22-i2v"
    model.mkdir()
    (model / "config.json").write_text("{}")
    monkeypatch.setenv("FASTVIDEO_MLX_VIDEO_HOME", str(home))
    monkeypatch.setenv("FASTVIDEO_MLX_VIDEO_MODEL", str(model))
    return home


def test_mlx_video_条件图只能落在作业目录里(tmp_path, monkeypatch):
    _fake_mlx_home(tmp_path, monkeypatch)

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

    # 绝对路径以前一路放行：runners.py 文件头的第 3 条硬约束写着"不接受界面随手
    # 填一个 /etc/passwd"，代码却没执行它。worker 是拿这台机器的权限起进程的，
    # 任何能提交作业的人都能借它读机器上任意一个文件。
    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                      {"image": "/etc/passwd"}, outputs)

    # 同一个文件换成绝对写法递进来仍然合法：边界看的是解析后的位置，不是写法
    abs_spec = runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                             {"image": str(image)}, outputs)
    assert abs_spec.argv[abs_spec.argv.index("--image") + 1] == str(image.resolve())

    # 目录里放一条指向外面的软链接：跟过去之后不在作业目录内，一样要拒
    (job_dir / "evil.png").symlink_to("/etc/passwd")
    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                      {"image": "inputs/evil.png"}, outputs)


def test_mlx_video_认得产线注入的上游产物(tmp_path, monkeypatch):
    """别的作业目录里的关键帧必须放行，但只认"平台自己产的那一类路径"。

    这一条补的是上一轮的实测缺陷：`pipeline._upstream_image` 会把上游关键帧的
    产物**绝对路径**注入 `image` 参数，那个文件在 `data/jobs/<上游作业>/outputs/`
    下，而边界只认"本作业目录"。结果本机接上 mlx-video 之后，每个要条件图的
    video 节点都在起跑前被判"条件图路径越出作业目录"——不是越权，是自己人
    被自己的闸门挡住。原来的测试只递本作业目录里的图，所以一直看不见这一档。
    """
    _fake_mlx_home(tmp_path, monkeypatch)

    job_dir = config.JOBS_DIR / "7" / "outputs"
    job_dir.mkdir(parents=True, exist_ok=True)
    outputs = tmp_path / "out"
    outputs.mkdir()

    # ① 上游作业的产物：同一种"平台产的文件"，只是不在本作业名下
    sibling = config.JOBS_DIR / "3" / "outputs"
    sibling.mkdir(parents=True, exist_ok=True)
    art = sibling / "keyframe.png"
    art.write_bytes(b"\x89PNG\r\n\x1a\n")
    # 上游作业必须与本作业同项目（第 2 条加的归属判据）。产线的上下游本就
    # 属于同一个项目，所以这一档照旧放行。查库那一跳用替身：本文件是纯
    # 单元测试，不为它拉数据库夹具；归属判据本身见本文件末尾那条。
    monkeypatch.setattr(runners, "_job_project_of", lambda job_id: 1)
    spec = runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                         {"image": str(art)}, outputs)
    assert spec.argv[spec.argv.index("--image") + 1] == str(art.resolve())

    # ② 但"在 data/jobs 下面"不是万能钥匙：第一层目录名必须是作业号（纯数字）。
    #    谁往 data/jobs/ 里丢一个随手命名的目录，不能因此变成合法输入图。
    side = config.JOBS_DIR / "scratch"
    side.mkdir(parents=True, exist_ok=True)
    (side / "note.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                      {"image": str(side / "note.png")}, outputs)

    # ③ 软链接伪装成作业目录里的文件：resolve 之后在圈外，照旧拒
    (sibling / "link.png").symlink_to("/etc/passwd")
    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                      {"image": str(sibling / "link.png")}, outputs)


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


def test_别的项目的产物不能拿来当输入图(tmp_path, monkeypatch):
    """第 2 条：`data/jobs/<作业号>/…` 的形状对了，**还得是同项目**的作业。

    原来这一档只判形状（`data/jobs/` 下且第一层是纯数字），于是任何人提交
    作业时把 `image` 填成别人作业的输出路径，就能把别人的产物读进自己的
    作业——跨项目读画稿、读未发布的成片。这条测试把"归属"钉住：
    同一个路径，同项目放行、跨项目拒绝。
    """
    _fake_mlx_home(tmp_path, monkeypatch)
    outputs = tmp_path / "out"
    outputs.mkdir()

    sibling = config.JOBS_DIR / "42" / "outputs"
    sibling.mkdir(parents=True, exist_ok=True)
    art = sibling / "keyframe.png"
    art.write_bytes(b"\x89PNG\r\n\x1a\n")

    # ① 作业 42 属于项目 9，而本作业属于项目 1 → 跨项目，必须拒。
    #    （`_job()` 默认 project_id=1、id=7）
    monkeypatch.setattr(runners, "_job_project_of", lambda job_id: 9)
    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                      {"image": str(art)}, outputs)

    # ② 同一路径，作业 42 属于本项目 → 放行（产线的正常形态）
    monkeypatch.setattr(runners, "_job_project_of", lambda job_id: 1)
    spec = runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                         {"image": str(art)}, outputs)
    assert spec.argv[spec.argv.index("--image") + 1] == str(art.resolve())

    # ③ 查不到那个作业（号是编的／已被删）一律拒——默认拒绝
    monkeypatch.setattr(runners, "_job_project_of", lambda job_id: None)
    with pytest.raises(runners.RunnerUnavailable):
        runners.build(_job(provider_code="mlx-video", model_code="wan2.2-i2v-a14b"),
                      {"image": str(art)}, outputs)
