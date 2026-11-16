"""Worker 日志边界：命令副本与真实子进程输出均不得记录正文、密钥和绝对路径。

命令测试检查 _run_spec 写出的实际日志及 resources.command，真实 argv 保持完整。
流式测试起真实 Python 子进程、强制七字节读取，验证 UTF-8、跨块正文／密钥、
路径、超长行和无换行尾部。撤销调用点过滤或密钥传递时，对应回归必须验红。
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import config                                                    # noqa: E402
import runners                                                   # noqa: E402
import worker                                                    # noqa: E402

# 产物必须落在 `JOBS_DIR` 之下：`jobfiles.relative()` 只认这个根，
# 否则 `_run_spec` 收尾时 `relative_to` 会抛 ValueError（不是我们要测的失败）。
OUTDIR: list[Path] = [config.JOBS_DIR]

# 一段足够长的"用户原创提示词"：超过截断长度，且是别处不会出现的串。
SECRET_PROMPT = ("一只穿着褪色红棉袄的老猫坐在结冰的河堤上，远处是雾里的旧厂房，"
                 "1950 年代东北工业区的黄昏，颗粒感，冷调，胶片质感")


class _FakeProc:
    """`subprocess.Popen` 的替身：不真起进程，只满足 `_run_spec` 的用法。"""

    def __init__(self, *args, **kwargs):
        self.args = args[0] if args else []
        self.pid = 424242
        self.stdout = _FakeStdout()
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


class _FakeStdout:
    def close(self):
        pass


@pytest.fixture
def run_spec_env(tmp_path, monkeypatch):
    """把"真起子进程"的三件事换成替身，其余（含日志写入）保持真实。"""
    seen = {}
    # 每个用例一个独立子目录，互不覆盖；必须在 JOBS_DIR 之下（见 OUTDIR 注释）。
    job_dir = config.JOBS_DIR / f"pytest-loghygiene-{tmp_path.name}"
    job_dir.mkdir(parents=True, exist_ok=True)
    # `_run_spec` 结尾要走 `runners.verify()`：产物必须存在、非空、
    # 且带 ISO-BMFF 的 `ftyp` 魔数。真进程会写出它，替身不会，
    # 所以这里预放一个"头看起来像 mp4"的最小文件。
    (job_dir / "out.mp4").write_bytes(
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41")

    def _fake_popen(argv, **kwargs):
        # 记下**真交给内核**的 argv：它必须原样保留完整提示词。
        seen["spawn_argv"] = list(argv)
        return _FakeProc(argv)

    class _FakeWatch:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return self

        def stop(self):
            return 0

    monkeypatch.setattr(worker.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(worker, "_RssWatch", _FakeWatch)
    monkeypatch.setattr(worker, "_pump", lambda *a, **k: None)
    monkeypatch.setattr(worker, "WORKER_MAX_RUNTIME_SECONDS", 60, raising=False)
    monkeypatch.setattr(runners, "provider_receipt", lambda spec: {})
    monkeypatch.setattr(runners, "metrics_summary", lambda p: {})
    log_path = tmp_path / "run.log"
    log_path.write_text("", encoding="utf-8")
    OUTDIR[0] = job_dir
    return log_path, seen


def _spec(prompt: str) -> runners.RunSpec:
    return runners.RunSpec(
        runner="mlx-video",
        argv=["/usr/bin/python", "-m", "mlx_video.wan_2.generate",
              "--prompt", prompt, "--seed", "20261109", "--height", "480"],
        cwd=OUTDIR[0], expected=[OUTDIR[0] / "out.mp4"], media="video", meta={})


def test_日志里找不到完整提示词(run_spec_env):
    """判据是**写出来的文件**里没有原文，不是"调了某个函数"。"""
    log_path, _seen = run_spec_env
    extra: dict = {}
    with log_path.open("a", encoding="utf-8") as log:
        worker._run_spec({"id": 1, "prompt_snapshot": SECRET_PROMPT,
                          "execution_mode": "local"}, _spec(SECRET_PROMPT),
                         log, "wid", extra)
    text = log_path.read_text(encoding="utf-8")
    assert SECRET_PROMPT not in text, "run.log 里躺着完整提示词"
    assert "--prompt" in text, "把这一格整个删了：排查要看'当时带了提示词、多长'"
    assert "共" in text and str(len(SECRET_PROMPT)) in text, "摘要里没有字数"


def test_resources_里找不到完整提示词(run_spec_env):
    """`extra["command"]` 会随 `_resources()` 进 `resources_json` 落库。"""
    log_path, _seen = run_spec_env
    extra: dict = {}
    with log_path.open("a", encoding="utf-8") as log:
        worker._run_spec({"id": 1, "prompt_snapshot": SECRET_PROMPT,
                          "execution_mode": "local"}, _spec(SECRET_PROMPT),
                         log, "wid", extra)
    dumped = json.dumps(extra.get("command"), ensure_ascii=False)
    assert SECRET_PROMPT not in dumped, "resources.command 里躺着完整提示词"
    assert "--seed" in extra["command"] and "20261109" in extra["command"], (
        "脱敏伤到了别的参数：种子是排查要用的")


def test_真交给子进程的_rgv_是完整的(run_spec_env):
    """脱敏只该碰"记录"，不该碰"执行"。

    这条是防以后有人图省事在记录函数里原地改：那样子进程收到的是截断后的
    提示词，出图照着摘要画——那是数据损坏，比日志里多一段字严重得多。
    """
    log_path, seen = run_spec_env
    extra: dict = {}
    with log_path.open("a", encoding="utf-8") as log:
        worker._run_spec({"id": 1, "prompt_snapshot": SECRET_PROMPT,
                          "execution_mode": "local"}, _spec(SECRET_PROMPT),
                         log, "wid", extra)
    assert SECRET_PROMPT in seen["spawn_argv"], "交给子进程的提示词被截断了"


def test_短提示词同样不进入日志(run_spec_env):
    """正文保护不取决于长度；短正文也只记录字数。"""
    log_path, _seen = run_spec_env
    extra: dict = {}
    short = "1girl, solo"
    with log_path.open("a", encoding="utf-8") as log:
        worker._run_spec({"id": 1, "prompt_snapshot": short,
                          "execution_mode": "local"}, _spec(short),
                         log, "wid", extra)
    text = log_path.read_text(encoding="utf-8")
    assert short not in text
    assert str(len(short)) in text
    assert short not in json.dumps(extra.get("command"))


def test_log_sanitize_masks_secrets_and_truncates(monkeypatch):
    """M1: log.sanitize_msg 对 sk-/Bearer/env 密钥脱敏并截断超长消息。"""
    import log as app_log

    monkeypatch.setenv("FASTVIDEO_TEST_API_KEY", "env-secret-token-xyz-999")
    raw = (
        "call failed Bearer my-bearer-secret-123456 "
        "key=sk-proj-secret1234567890abcdef "
        "env=env-secret-token-xyz-999 "
        + ("A" * 400)
    )
    cleaned = app_log.sanitize_msg(raw, max_len=160)
    assert "my-bearer-secret-123456" not in cleaned
    assert "sk-proj-secret1234567890abcdef" not in cleaned
    assert "env-secret-token-xyz-999" not in cleaned
    assert "A" * 200 not in cleaned


def test_main_unhandled_error_returns_sanitized_500(caplog):
    """M1: main 未捕获异常兜底返回脱敏 JSON 500，不向响应泄露密钥。"""
    from fastapi.testclient import TestClient
    import main

    app = main.create_app()

    @app.get("/_test_m1_boom")
    def _boom():
        raise RuntimeError("boom sk-leak-secret-1234567890abcdef")

    app.router.routes.insert(0, app.router.routes.pop())

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level("ERROR"):
        resp = client.get("/_test_m1_boom")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "内部服务异常，请查看运行日志"}
    assert "sk-leak-secret-1234567890abcdef" not in resp.text
    assert "sk-leak-secret-1234567890abcdef" not in caplog.text


def test_worker_and_agents_facade_exports():
    """M1: worker 与 agents 拆包后保留完整门面符号。"""
    import agents

    for sym in ("_RssWatch", "descendant_peak", "_pump", "run_once", "main"):
        assert hasattr(worker, sym), f"worker facade missing {sym}"
    for sym in ("create_run", "EVENT_LABEL", "tick", "plan_route", "loads"):
        assert hasattr(agents, sym), f"agents facade missing {sym}"



def test_记录命令与工作目录不泄露绝对路径(run_spec_env):
    log_path, seen = run_spec_env
    spec = _spec("private short prompt")
    spec.argv.extend(["--input=/server/private/source.png", "--negative=private negative"])
    extra = {}
    with log_path.open("a", encoding="utf-8") as log:
        worker._run_spec({"id": 1, "execution_mode": "local"}, spec, log, "wid", extra)
    recorded = log_path.read_text() + json.dumps(extra.get("command"))
    for private in (str(spec.cwd), "/usr/bin/python", "/server/private", "private short prompt", "private negative"):
        assert private not in recorded
    assert seen["spawn_argv"] == spec.argv
    assert "--seed" in recorded and "20261109" in recorded


def test_真实子进程分块输出同样脱敏(tmp_path, monkeypatch):
    import subprocess
    import time
    prompt = "隐私短正文\n第二行创作内容"
    negative = "不应进入日志的负向正文"
    database_key = "db-only-value-123"
    environment_key = "env-only-value-789"
    monkeypatch.setenv("ROUND3_API_KEY", environment_key)
    text = (prompt + "\n" + json.dumps(prompt, ensure_ascii=False) + "\n" + negative + "\n" +
            database_key + "\n" + environment_key + "\n/server/private/model.ckpt\n" +
            "x" * 9000 + "\n进度 50%\rhttps://example.test/status\n末尾完成")
    proc = subprocess.Popen([sys.executable, "-c", f"import sys;sys.stdout.buffer.write({text.encode()!r})"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    monkeypatch.setattr(worker, "STOP", False)
    for method in ("status_of", "renew", "heartbeat"):
        monkeypatch.setattr(worker.jobs, method, lambda *args, **kwargs: None)
    original_read = worker.os.read
    monkeypatch.setattr(worker.os, "read", lambda fd, size: original_read(fd, min(size, 7)))
    path = tmp_path / "stream.log"
    try:
        with path.open("w", encoding="utf-8") as log:
            worker._pump(proc, log, {"id": 123, "prompt_snapshot": prompt,
                         "params_json": json.dumps({"negative_prompt": negative})}, "test",
                         deadline=time.monotonic() + 10, private_values=(database_key,))
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            worker._terminate(proc, grace=1)
        proc.stdout.close()
    recorded = path.read_text()
    for private in (*prompt.splitlines(), negative, database_key, environment_key, "/server/private"):
        assert private not in recorded
    assert "进度 50%" in recorded and "末尾完成" in recorded
    assert "https://example.test/status" in recorded
    assert "超长输出已隐藏" in recorded and "x" * 100 not in recorded
    assert "\ufffd" not in recorded


def test_执行路径将数据库密钥交给输出脱敏边界(run_spec_env, monkeypatch):
    log_path, _ = run_spec_env
    from dataclasses import replace
    key = "synthetic-database-key"
    spec = replace(_spec("正文"), pass_env=("ROUND3_KEY",))
    seen = {}
    monkeypatch.setattr(worker, "_child_env", lambda _: {"ROUND3_KEY": key})
    def pump(*args, **kwargs):
        seen.update(kwargs)
    monkeypatch.setattr(worker, "_pump", pump)
    with log_path.open("a", encoding="utf-8") as log:
        worker._run_spec({"id": 1, "execution_mode": "local"}, spec, log, "wid", {})
    assert seen["private_values"] == (key,)
    assert key not in log_path.read_text()
