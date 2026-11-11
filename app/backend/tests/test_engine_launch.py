# -*- coding: utf-8 -*-
"""本机引擎「启动」按钮的三道边界（2026-11-11 加）。

这一族的风险与别处不同：它真的会 `subprocess.Popen`。所以用例的重点不是
「能不能起来」，而是「什么时候必须一条命令都不执行」：

  1. 非回环来源必须 403——`FASTVIDEO_HOST=0.0.0.0` 那种部署下，
     点一下按钮在你机器上起进程是越权的；
  2. 没有配方的引擎码必须拒绝——配方是常量，不是可写数据；
  3. 已经在答话的服务必须不重复启动——`ollama serve` 会真的抢端口；
  4. 「进程起来了」与「端口答话了」必须分开报——Draw Things 的 API Server
     是应用内开关，应用起来照样连不上。

一条底线：**任何一条用例都不许真的把服务拉起来**。`subprocess.Popen` 一律
monkeypatch 成假函数，并且断言它有没有被调用——「403 那条路径一次都没执行
命令」这件事只有数调用次数才证明得了。
"""
from __future__ import annotations

import pytest

import engine_launch
import engines
from repositories.engine_instances import engine_instances

SLOT = "http://127.0.0.1:7860"


@pytest.fixture
def spawns(monkeypatch):
    """把 Popen 换成记账的假函数。返回那个 list，用例自己数。"""
    calls: list[list[str]] = []

    class _Fake:
        pass

    def fake_popen(argv, **kwargs):
        calls.append(list(argv))
        # start_new_session 是这一支的关键参数：人拉起来的是要长期驻留的服务，
        # 不能挂在 API 进程上（API 重启会顺带杀掉它）。用例一并钉住它。
        assert kwargs.get("start_new_session") is True, kwargs
        return _Fake()

    monkeypatch.setattr(engine_launch.subprocess, "Popen", fake_popen)
    return calls


@pytest.fixture
def offline(monkeypatch):
    """端点一律「连不上」——本机 CI 上这两个服务本来就不在跑。"""
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_: engines.EngineStatus(
        "missing", "连不上：Connection refused",
        {"endpoint": endpoint or SLOT}))


def _instance(engine_code="draw_things", endpoint=SLOT):
    return engine_instances.create({"engine_code": engine_code, "endpoint": endpoint})


# ---------- 第 1 道：来源 ----------

def test_非回环来源不执行任何命令(monkeypatch, as_role, spawns, offline):
    """开放到局域网的部署下，这条必须是 403，且一次命令都没跑。

    `TestClient` 的 client.host 是 `testclient`，天然不是回环——正好就是本机
    之外的来源。这条同时是「默认拒绝」的证据：忘了删调试代码也过不去。
    """
    row = _instance()
    try:
        resp = as_role("admin").post(f"/api/engines/{row['id']}/launch")
        assert resp.status_code == 403, resp.text
        assert "这台机器" in resp.json()["detail"]
        assert spawns == [], f"被拒的请求执行了命令：{spawns}"
    finally:
        engine_instances.delete(row["id"])


def test_回环来源才放行(monkeypatch, as_role, spawns, offline):
    """把来源改成 127.0.0.1 之后才走到配方那一步。"""
    row = _instance()
    try:
        monkeypatch.setattr(engine_launch, "LOOPBACK", frozenset({"testclient"}))
        monkeypatch.setattr(engine_launch, "WAIT_SECONDS", 0.0)
        resp = as_role("admin").post(f"/api/engines/{row['id']}/launch")
        assert resp.status_code == 200, resp.text
        assert [c[0] for c in spawns] == ["open"], spawns
    finally:
        engine_instances.delete(row["id"])


# ---------- 第 2 道：配方 ----------

def test_没有配方的引擎码一条命令都不执行(monkeypatch, offline, spawns):
    """配方按 engine_code 查表；查不到就拒绝，绝不兜底执行别的东西。"""
    result = engine_launch.launch("not_registered_engine", SLOT)
    assert result.ok is False
    assert "没有内置启动配方" in result.message
    assert spawns == [], f"没配方的引擎码执行了命令：{spawns}"


def test_配方里的命令是常量而不是命令字符串():
    """防止有人把配方改成 `["sh", "-c", 用户输入]` 那种形状。

    这一条不是格式洁癖：`0040_engine_instance.sql:34` 那句 CR-001 禁止的就是
    「可执行入口」变成可写字段。配方一旦接受拼接，这条接口就成了通用命令执行。
    """
    for code, recipe in engine_launch.RECIPES.items():
        argv = recipe["argv"]
        assert isinstance(argv, list) and argv, code
        assert all(isinstance(a, str) for a in argv), code
        assert argv[0] not in ("sh", "bash", "zsh", "/bin/sh"), \
            f"{code} 的配方用 shell 包了一层：这会把注入面打开"
        joined = " ".join(argv)
        assert "{" not in joined and "$" not in joined, \
            f"{code} 的配方里出现了插值占位：命令必须是写死的常量"


# ---------- 第 3 道：不重复启动 ----------

def test_已经在答话就不重复启动(monkeypatch, spawns):
    """端口已经通了就别再起一个——`ollama serve` 会真的抢 11434。"""
    monkeypatch.setattr(engines, "probe", lambda code, endpoint="", **_: engines.EngineStatus(
        "ok", "服务在线", {"endpoint": endpoint or SLOT, "model": "x", "keys": 1}))
    result = engine_launch.launch("ollama", SLOT)
    assert result.ok is True
    assert result.started is False, "已经在跑却报了一次启动"
    assert result.reachable is True
    assert spawns == [], f"已经在跑还执行了命令：{spawns}"


# ---------- 第 4 道：起来 ≠ 能用了 ----------

def test_起了但没答话要说清楚不是能用了(monkeypatch, spawns, offline):
    """Popen 成功只代表命令发出去了。端口不答话必须如实分开报。

    这就是 Draw Things 的真实形状：API Server 是应用内开关，默认关。
    """
    monkeypatch.setattr(engine_launch, "WAIT_SECONDS", 0.0)
    result = engine_launch.launch("draw_things", SLOT)
    assert len(spawns) == 1, spawns
    assert result.started is True
    assert result.reachable is False, "没答话却报了 reachable"
    assert result.ok is False
    # 必须带上「开关默认关」这条能动手的提示，而不是一句「失败了」
    assert "API Server" in result.message, result.message


def test_命令没装时给出能动手的提示而不是抛栈(monkeypatch, spawns):
    monkeypatch.setattr(engine_launch.shutil, "which", lambda _: None)
    result = engine_launch.launch("ollama", SLOT)
    assert result.ok is False and result.started is False
    assert "不在 PATH" in result.message, result.message
    assert spawns == [], "命令不在 PATH 上却还是执行了"


def test_起来并答话了才留痕(monkeypatch, as_role):
    """留痕表记的是「这个端点被问过」。没答话的那几次不往历史里塞半截结论。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(engine_launch.subprocess, "Popen",
                        lambda argv, **_: calls.append(list(argv)))

    state = {"up": False}

    def fake_probe(code, endpoint="", **_) -> engines.EngineStatus:
        # 第一次（启动前）说连不上；命令跑过之后再说通——模拟真实时序
        return engines.EngineStatus(
            "ok" if state["up"] else "missing",
            "服务在线" if state["up"] else "连不上",
            {"endpoint": endpoint or SLOT, "model": "x", "keys": 1})

    orig_popen = engine_launch.subprocess.Popen

    def popen_and_up(argv, **kw):
        orig_popen(argv, **kw)
        state["up"] = True

    monkeypatch.setattr(engine_launch.subprocess, "Popen", popen_and_up)
    monkeypatch.setattr(engines, "probe", fake_probe)
    monkeypatch.setattr(engine_launch, "LOOPBACK", frozenset({"testclient"}))
    monkeypatch.setattr(engine_launch, "WAIT_SECONDS", 2.0)
    monkeypatch.setattr(engine_launch, "POLL_EVERY", 0.01)

    from repositories.engine_instances import engine_probe_log
    row = _instance("ollama", "http://127.0.0.1:11434")
    try:
        before = len(engine_probe_log.recent(row["id"]))
        resp = as_role("admin").post(f"/api/engines/{row['id']}/launch")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["started"] is True and body["reachable"] is True, body
        after = engine_probe_log.recent(row["id"])
        assert len(after) == before + 1, "起来并答话之后没有留痕"
        # 记 manual 而不是新开 launch 档（0042 的 CHECK 只认 manual/create，
        # 加第三档要重建只追加表，收益不抵代价）
        assert after[0]["source"] == "manual", after[0]
    finally:
        engine_instances.delete(row["id"])
