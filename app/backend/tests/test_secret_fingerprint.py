"""云端密钥的指纹：**能确认是哪一把，推不回原值**（2026-11-11）。

这一组测的是「不泄漏」，不是 CRUD：
  * 指纹里不能出现密钥的任何随机段（第一版写错过，实测把真实值前 12 位
    印在了页面上）；
  * 只露供应商公开的家族标记（`sk-` 这类，公开文档里写着的）；
  * 非 `system:config` 的人一个字段都拿不到。
"""
from __future__ import annotations

import hashlib

import pytest

import adapters

KEY = "sk-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0"


def test_指纹不含密钥的任何片段(monkeypatch):
    """**这是整个功能的安全底线**：算出来的东西不能让人反推出密钥。

    判据：把密钥按 4/6/8/12/16 字符切窗，**任何一个窗口都不能出现在指纹里**。
    只检查"完整密钥不出现"是不够的——第一次实现里 `preview` 露出了
    `sk-a1b2c3d4e`（真实值的前 12 段），完整密钥当然不在里面，但那是泄漏。
    """
    monkeypatch.setenv("FASTVIDEO_TEST_KEY", KEY)
    fp = adapters.key_fingerprint("FASTVIDEO_TEST_KEY")

    assert fp["present"] is True
    blob = f"{fp['fingerprint']}{fp['preview']}"
    for n in (4, 6, 8, 12, 16):
        for i in range(0, len(KEY) - n + 1):
            assert KEY[i:i + n] not in blob, (
                f"指纹里出现了密钥的 {n} 字符片段 {KEY[i:i+n]!r}——"
                f"这让人能暴力补全剩下的部分：{fp}")


def test_只露公开的供应商家族标记(monkeypatch):
    """`sk-`／`sk-ant-` 这些是公开文档里写着的名字，露出来不算秘密。

    但要**精确**只露这些：`sk-ant-api03` 里的 `api03` 是公开版本号可以露，
    而 `sk-abcdefg` 里的 `abcdefg` 是随机段，一个字符都不许露。
    这里用"露出的部分必须完全等于某个白名单标记"来钉死。
    """
    cases = {
        "sk-RANDOMSECRETPART": "sk-",
        "sk-ant-api03-RANDOMSECRET": "sk-ant-",
        "sk-or-v1-RANDOMSECRET": "sk-or-",
        "sk-proj-RANDOMSECRET": "sk-proj-",
        "AKIARANDOMSECRET": "",          # AWS 风格，认不出就不露
        "randomplainvalue": "",           # 认不出就不露
    }
    for value, expect in cases.items():
        monkeypatch.setenv("FASTVIDEO_TEST_KEY", value)
        fp = adapters.key_fingerprint("FASTVIDEO_TEST_KEY")
        shown = fp["preview"].split("…")[0]
        assert shown == expect, (
            f"{value!r} 露出了 {shown!r}，期望 {expect!r}——"
            f"露出的部分必须是公开标记，不能含密钥随机段")


def test_指纹是哈希前八位且稳定(monkeypatch):
    """同值同指纹（可比对），异值不同指纹（能区分），长度照实报。"""
    monkeypatch.setenv("FASTVIDEO_TEST_KEY", KEY)
    a = adapters.key_fingerprint("FASTVIDEO_TEST_KEY")
    b = adapters.key_fingerprint("FASTVIDEO_TEST_KEY")
    assert a == b, "同一个值算出两个指纹，比对就失效了"
    assert a["fingerprint"] == hashlib.sha256(KEY.encode()).hexdigest()[:8]
    assert a["length"] == len(KEY)

    monkeypatch.setenv("FASTVIDEO_TEST_KEY", KEY + "x")
    c = adapters.key_fingerprint("FASTVIDEO_TEST_KEY")
    assert c["fingerprint"] != a["fingerprint"], "不同值撞了指纹，无法区分"
    assert c["length"] == len(KEY) + 1


def test_没配置时不编造指纹(monkeypatch):
    """没配就是 `present: False`，不给一个假的空指纹。

    给空串会让界面显示成「已配置但指纹为空」，那是最难查的一种假象。
    """
    monkeypatch.delenv("FASTVIDEO_TEST_KEY", raising=False)
    fp = adapters.key_fingerprint("FASTVIDEO_TEST_KEY")
    assert fp == {"present": False}


@pytest.fixture()
def cloud_registered():
    """登记一条云端模型，让 `/api/secrets` 有东西可列。

    conftest 的临时库是空的（没有现役库那批 0035 自动登记的行），
    所以这里自己摆事实——**依赖"库里本来就有"的测试在 CI 上必然红**，
    那是这一组第一版踩到的：断言 `items` 非空却拿到 `[]`。
    """
    from db import get_conn
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO model_registration
                   (capability, backend, provider, model_code, display_name,
                    source, engine_code, enabled, note)
               VALUES ('text', 'cloud', 'deepseek', 'deepseek-flash', '',
                       'cloud', 'deepseek', 1, '测试登记')""")
    yield
    with get_conn() as conn:
        conn.execute("DELETE FROM model_registration WHERE note = '测试登记'")


def test_接口只给指纹不给值(as_role, monkeypatch, cloud_registered):
    """整条 HTTP 路径上都不能出现密钥值——包括错误信息与备注文案。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    client = as_role("admin")
    r = client.get("/api/secrets")
    assert r.status_code == 200, r.text
    body = r.text
    assert KEY not in body, "响应体里出现了完整密钥"
    for n in (8, 12, 16):
        assert KEY[:n] not in body, f"响应体里出现了密钥前 {n} 位"

    items = r.json()["items"]
    assert items, "已登记 deepseek，/api/secrets 却一项都没有"
    deep = [i for i in items if i["env_name"] == "DEEPSEEK_API_KEY"][0]
    assert deep["present"] is True
    assert deep["fingerprint"] and deep["length"] == len(KEY)


def test_普通人看不到密钥指纹(as_role, provider_state):
    """`editor` 连「有没有配」都不该看到：它能推出"这台机器有供应商账号"。"""
    client = as_role("editor")
    assert client.get("/api/secrets").status_code == 403
    assert client.post("/api/secrets/deepseek/test").status_code == 403


def test_备注必须说明改完要重启_worker(as_role, provider_state):
    """这个错配是让人怀疑平台坏了的第一来源，文案不能省。

    API 进程读 key 是**实时**的（`os.environ` 现读），worker 却是 fork 快照：
    这一页会立刻变「已配置」，而云端作业仍报「环境变量 X 没有值」。
    """
    client = as_role("admin")
    note = client.get("/api/secrets").json()["note"]
    assert "重启" in note and "worker" in note, note
