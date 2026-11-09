"""`run.log` 与 `resources_json` 里不落完整提示词（2026-11-09 体检 B2）。

## 原来是什么样

`worker._run_spec` 做两件事：

    _write(log, "命令：" + json.dumps(spec.argv, ensure_ascii=False))
    extra["command"] = spec.argv          # → _resources() → resources_json 落库

而 `runners.py` 把 `job["prompt_snapshot"]` 直接拼进 argv（`:123` `:136`
`:223` `:261`，以及 engine_http/llm 那条走请求文件）。于是**用户写的整段创作
提示词**同时躺在两个地方：作业日志（`GET /jobs/{id}/logs` 原样回）与
`resources_json`（跟着 Job 落库、回给界面）。

## 这条算不算安全问题

**不算越权**：日志与提示词走同一个 `job:read` 权限，能读日志的人本来就能读提示词，
所以没有"低权限者看到高敏内容"这一层。

算的是**卫生**：日志的实际用法是"出问题贴进工单/发给外部排查"。贴出去的时候，
模型档位、种子、帧数、耗时才是排查要的；整段创作正文既没用，又是白送的创作内容。
另外同一个字串在两处各存一份，改口径时容易只改一处。

## 为什么这一版测试直接调 `_run_spec`

第一版只测了辅助函数 `_argv_for_record`，反向验证时把 `_run_spec` 改回裸
`spec.argv` 仍然全绿——那种测试测的是"我加了个函数"，不是"落盘产物里没原文"。
现在把 `subprocess.Popen` 换成替身，让 `_run_spec` 真跑一遍，
再从**它写出的日志文件**和**它填的 extra** 里找原文。反向验证：改回裸
`spec.argv`，本文件立刻红。
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


def test_短提示词原样保留(run_spec_env):
    """`1girl, solo` 这种本来就不长，换成摘要反而更难读。"""
    log_path, _seen = run_spec_env
    extra: dict = {}
    short = "1girl, solo"
    with log_path.open("a", encoding="utf-8") as log:
        worker._run_spec({"id": 1, "prompt_snapshot": short,
                          "execution_mode": "local"}, _spec(short),
                         log, "wid", extra)
    text = log_path.read_text(encoding="utf-8")
    assert short in text, "短提示词被无谓地折叠了"
