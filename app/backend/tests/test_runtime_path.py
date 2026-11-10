"""本地运行时路径可以网页配置（0051）——但**密钥永远不行**。

这一组测的是边界，不是 CRUD：
  * 密钥类变量名必须进不来（两道各自独立：migration 的 CHECK + 仓储的白名单）；
  * 环境变量必须优先于网页（显式配置不能被页面悄悄盖掉）；
  * "改了没生效"必须能说清是哪一种（没重启 / 被环境占住 / 没配）。
"""
from __future__ import annotations

import pytest

from repositories import runtime_path


@pytest.fixture()
def clean_paths():
    """每个用例自己收拾：这张表是全局单例，串味会让断言互相污染。"""
    runtime_path.forget()
    yield
    runtime_path.forget()


def test_密钥类变量一个都进不来(clean_paths):
    """这是整个功能的红线：密钥只走环境变量，不进库。

    两道独立的门都要验：仓储的白名单拦住写请求，migration 的 CHECK 拦住
    **绕过仓储的直接 SQL**。只测一道的话，另一道失效时没人知道。
    """
    import sqlite3
    from db import get_conn

    keys = ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "FV_KEY_VOLC",
            "OPENAI_API_KEY", "SOME_TOKEN", "ANY_SECRET")

    # 门一：仓储白名单
    for name in keys:
        with pytest.raises(ValueError):
            runtime_path.save(name, "sk-whatever")

    # 门二的另一半：白名单外的**非密钥**名字也必须被拒。
    # 没有这一条时，上面那批全被拒可能是"密钥特征检查"独自拦下的——
    # 而白名单那一半可能已经被改坏了却没人知道（实测变异：把
    # `if env_name not in ALLOWED_ENV` 改成 `if False`，只测密钥名的话
    # 6 条用例**全绿**，因为密钥特征检查兜住了）。
    with pytest.raises(ValueError):
        runtime_path.save("FASTVIDEO_NOT_A_REAL_VAR", "/x")

    # 门二：migration 的 CHECK（绕过仓储，直接写库）
    for name in keys:
        with pytest.raises(sqlite3.IntegrityError):
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO runtime_path (env_name, path) VALUES (?, ?)",
                    (name, "sk-whatever"))

    # 反证：合法路径必须能写（否则上面那条"全部被拒"可能是白名单被写成了空）
    assert runtime_path.save("FASTVIDEO_MFLUX_HOME", "/opt/mflux") == "/opt/mflux"
    assert runtime_path.get_one("FASTVIDEO_MFLUX_HOME") == "/opt/mflux"


def test_环境变量优先于网页(clean_paths, monkeypatch):
    """部署时显式导出的环境变量，不能被网页上的值盖掉。

    这是"来源要说清"的前提：如果网页能盖环境，运维就永远查不清
    "为什么我明明 export 了却不生效"。`locked=True` 就是页面上的那句提示。
    """
    import runtime_paths

    monkeypatch.setenv("FASTVIDEO_VOXCPM_HOME", "/from/env")
    runtime_path.save("FASTVIDEO_VOXCPM_HOME", "/from/web")

    item = [i for i in runtime_paths.describe()
            if i["env_name"] == "FASTVIDEO_VOXCPM_HOME"][0]
    assert item["env_value"] == "/from/env"
    assert item["saved_value"] == "/from/web"
    assert item["effective"] == "/from/env", "网页值盖掉了环境变量"
    assert item["source"] == "env"
    assert item["locked"] is True

    # apply() 也不许覆盖它
    written = runtime_paths.apply()
    assert "FASTVIDEO_VOXCPM_HOME" not in written
    import os
    assert os.environ["FASTVIDEO_VOXCPM_HOME"] == "/from/env"


def test_没有环境变量时网页值生效(clean_paths, monkeypatch):
    """配对用例：没被环境占住时，网页上设的值必须真的生效。

    少了这一条，"环境优先"可以靠"什么都优先"实现——那样网页整体就是死的。
    """
    import runtime_paths

    monkeypatch.delenv("FASTVIDEO_MFLUX_HOME", raising=False)
    runtime_path.save("FASTVIDEO_MFLUX_HOME", "/opt/mflux")

    item = [i for i in runtime_paths.describe()
            if i["env_name"] == "FASTVIDEO_MFLUX_HOME"][0]
    assert item["effective"] == "/opt/mflux"
    assert item["source"] == "web"
    assert item["locked"] is False

    assert "FASTVIDEO_MFLUX_HOME" in runtime_paths.apply()
    import os
    assert os.environ["FASTVIDEO_MFLUX_HOME"] == "/opt/mflux"


def test_空串等于清除覆盖(clean_paths):
    """两种"没设"不该有两种表示：空串写入后归一到 None，`get_all` 里就不出现。"""
    runtime_path.save("FASTVIDEO_FASTMETAL_HOME", "/somewhere")
    assert "FASTVIDEO_FASTMETAL_HOME" in runtime_path.get_all()

    assert runtime_path.save("FASTVIDEO_FASTMETAL_HOME", "") is None
    assert "FASTVIDEO_FASTMETAL_HOME" not in runtime_path.get_all()
    assert runtime_path.get_one("FASTVIDEO_FASTMETAL_HOME") is None


def test_探测三档分得清(clean_paths, tmp_path, monkeypatch):
    """ok / missing / unknown 是三种**不同的人要做的事**，不能混成一句"不可用"。

      * unknown —— 没配（人要做的事：配一个）
      * missing —— 配了但路径不在（人要做的事：改路径，或把东西搬过去）
      * ok      —— 配了且东西在
    把 missing 说成 unknown，人会去配一个其实配错的地方。

    这里直接调路由层那个 `_runtime_state`（它就是接口返回 `state` 的来源）。
    只测 `describe()` 是不够的：它只给 `effective`，真正的 ok/missing 判定
    发生在 `_runtime_state`，而"配错了却显示没配"正是这条要防的错。
    """
    import routers.model_center as mc

    # unknown：没配
    assert mc._runtime_state("FASTVIDEO_MFLUX_HOME", None)[0] == "unknown"

    # missing：配了但目录不在
    state, detail = mc._runtime_state(
        "FASTVIDEO_MFLUX_HOME", str(tmp_path / "不存在"))
    assert state == "missing", detail
    assert "不存在" in detail

    # ok：真建出来
    real = tmp_path / "mflux"
    real.mkdir()
    assert mc._runtime_state("FASTVIDEO_MFLUX_HOME", str(real))[0] == "ok"

    # `_BIN` 判的是**文件**不是目录：判据不同，用名字区分
    binp = tmp_path / "mflux-generate"
    binp.write_text("#!/bin/sh\n")
    assert mc._runtime_state("FASTVIDEO_MFLUX_BIN", str(binp))[0] == "ok"
    # 同名但是个目录 → 不是可执行文件，必须 missing
    assert mc._runtime_state("FASTVIDEO_MFLUX_BIN", str(real))[0] == "missing"


def test_路径列表里没有任何密钥变量(clean_paths):
    """白名单本身的自检：这 10 个名字里不能混进一个密钥。

    元组是手写的，加新引擎时复制粘贴很容易把 `_KEY` 一起带进来。
    """
    for name in runtime_path.ALLOWED_ENV:
        upper = name.upper()
        assert "API_KEY" not in upper, f"{name} 看着是密钥变量，不该在路径白名单里"
        assert "TOKEN" not in upper and "SECRET" not in upper, name
    assert len(runtime_path.ALLOWED_ENV) == len(set(runtime_path.ALLOWED_ENV)), \
        "白名单里有重复项"
