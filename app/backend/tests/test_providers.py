"""W06 Provider 封版：密钥不外泄、默认唯一、不自动兜底、清单不猜参数。

这一组的重点是**反证**：故意把密钥放进环境变量，再证明它没出现在
响应、数据库、错误消息与日志里；故意只配一半，再证明解析器会拒绝
而不是偷偷换到另一条轨。
"""
import json

import pytest

import adapters
import services
from db import get_conn

SECRET = "sk-test-not-a-real-key-7f3a9"


@pytest.fixture
def provider_state(app):
    """provider_config 是全局单份的：动过就还原，免得后面的用例读到别人的配置。"""
    with get_conn() as conn:
        saved = [dict(r) for r in conn.execute("SELECT * FROM provider_config")]
    yield
    with get_conn() as conn:
        for r in saved:
            conn.execute(
                """UPDATE provider_config
                      SET provider = ?, model = ?, enabled = ?, is_default = ?,
                          key_env = ?, notes = ?
                    WHERE id = ?""",
                (r["provider"], r["model"], r["enabled"], r["is_default"],
                 r["key_env"], r["notes"], r["id"]),
            )


def _rows(c):
    out = c.get("/api/providers").json()
    assert len(out) == 10, out
    return out


def _payload(rows, **want):
    """把 GET 结果改成 PUT 需要的形状；want 用来点名要改的那一行。

    用法：_payload(rows, video_local={...})，键是 "<capability>_<backend>"。
    """
    body = []
    for r in rows:
        item = {
            "id": r["id"], "provider": r["provider"], "model": r["model"],
            "enabled": r["enabled"], "is_default": r["is_default"],
            "key_env": r["key_env_custom"], "notes": r["notes"],
        }
        item.update(want.get(f"{r['capability']}_{r['backend']}", {}))
        body.append(item)
    return body


def test_迁移后四类本地默认_文本留云端(as_role, provider_state):
    """规格 01-4.8.2：图像/视频/TTS/音乐默认本地；文本只有云端一条路。"""
    rows = _rows(as_role("admin"))
    defaults = {r["capability"]: r["backend"] for r in rows if r["is_default"]}
    assert defaults == {"text": "cloud", "image": "local", "video": "local",
                        "tts": "local", "music": "local"}, defaults
    assert all(v == "local" for k, v in defaults.items() if k != "text")


def test_密钥值从不进响应库和日志(as_role, provider_state, monkeypatch, caplog):
    monkeypatch.setenv("FV_KEY_MYPROV", SECRET)
    c = as_role("admin")
    rows = _rows(c)
    body = _payload(rows, text_cloud={
        "provider": "myprov", "model": "x-1", "enabled": True,
        "key_env": "FV_KEY_MYPROV", "notes": "测试用"})
    r = c.put("/api/providers", json=body)
    assert r.status_code == 200, r.text

    # 三处都不许出现值：响应、GET、库
    assert SECRET not in r.text
    assert SECRET not in c.get("/api/providers").text
    with get_conn() as conn:
        dump = json.dumps([dict(x) for x in conn.execute("SELECT * FROM provider_config")],
                          ensure_ascii=False)
    assert SECRET not in dump
    assert "FV_KEY_MYPROV" in r.text          # 变量名可以出现
    assert SECRET not in caplog.text


def test_清单建议名让_DEEPSEEK_API_KEY_直接可用(as_role, provider_state, monkeypatch):
    """既有密钥名不必改成 FV_KEY_*；key_env 留空时按清单建议走。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    c = as_role("admin")
    rows = _rows(c)
    r = c.put("/api/providers", json=_payload(rows, text_cloud={
        "provider": "deepseek", "model": "deepseek-chat", "enabled": True,
        "key_env": ""}))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["capability"] == "text" and x["backend"] == "cloud")
    assert row["key_env"] == "DEEPSEEK_API_KEY"
    assert row["key_env_custom"] == ""
    assert row["has_key"] is True
    assert SECRET not in r.text


def test_密钥变量名与_provider_标识要合法(as_role, provider_state):
    c = as_role("admin")
    rows = _rows(c)
    bad_env = _payload(rows, text_cloud={"key_env": "NOT-A-VAR"})
    assert c.put("/api/providers", json=bad_env).status_code == 422

    bad_provider = _payload(rows, text_cloud={"provider": "rm -rf /"})
    assert c.put("/api/providers", json=bad_provider).status_code == 422


def test_云端视频不能设成默认(as_role, provider_state):
    c = as_role("admin")
    rows = _rows(c)
    body = _payload(rows, video_local={"is_default": False},
                    video_cloud={"is_default": True})
    r = c.put("/api/providers", json=body)
    assert r.status_code == 422
    assert "云端视频" in r.json()["detail"]


def test_每类能力只能一个默认(as_role, provider_state):
    c = as_role("admin")
    rows = _rows(c)
    body = _payload(rows, image_cloud={"is_default": True})   # local + cloud 两个默认
    r = c.put("/api/providers", json=body)
    assert r.status_code == 422
    assert "只能有一个默认" in r.json()["detail"]


def test_备注往返与健康状态(as_role, provider_state):
    c = as_role("admin")
    rows = _rows(c)
    r = c.put("/api/providers", json=_payload(rows, image_local={
        "provider": "mflux", "model": "flux1-schnell", "enabled": True,
        "notes": "1024 方图，4 步"}))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["capability"] == "image" and x["backend"] == "local")
    assert row["notes"] == "1024 方图，4 步"
    # 本机没设 FASTVIDEO_MFLUX_HOME ⇒ 诚实地说"未检查"，不假装就绪
    assert row["health"]["state"] == "unknown"
    assert row["health"]["adapter"]["code"] == "mflux"


def test_未登记_provider_不给参数(as_role, provider_state):
    c = as_role("admin")
    rows = _rows(c)
    r = c.put("/api/providers", json=_payload(rows, text_cloud={
        "provider": "skywalker", "model": "x", "enabled": True}))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["capability"] == "text" and x["backend"] == "cloud")
    assert row["health"]["state"] == "unregistered"
    assert row["health"]["adapter"] is None
    assert adapters.find("text", "cloud", "skywalker") is None


def test_清单接口只登记不猜测(as_role):
    c = as_role("admin")
    r = c.get("/api/providers/manifest")
    assert r.status_code == 200
    data = r.json()
    assert data["policy"]["video_cloud_must_be_explicit"] is True
    assert data["policy"]["no_auto_fallback"] is True

    by_code = {(a["capability"], a["backend"], a["code"]): a for a in data["adapters"]}
    fast = by_code[("video", "local", "fastmetal")]
    assert fast["params_known"] is True
    assert any(m["code"] == "fastmetal-5b-qad" and m["params"] for m in fast["models"])
    # 别名也要随清单下发：界面按用户输入的 mlx_video 找得到 mlx-video
    assert "mlx_video" in by_code[("video", "local", "mlx-video")]["aliases"]
    assert adapters.find("video", "local", "mlx_video").code == "mlx-video"

    # 云端视频只登记了名字，参数一栏必须是空的——核过之前不写死
    for code in ("wan", "kling", "seedance"):
        a = by_code[("video", "cloud", code)]
        assert a["params_known"] is False
        assert a["models"] == []
        assert "未核验" in a["source"]


def test_current_给普通模块只读默认(as_role, provider_state):
    c = as_role("editor")            # 没有 system:config，也能读当前默认
    r = c.get("/api/providers/current")
    assert r.status_code == 200
    rows = r.json()
    assert [x["capability"] for x in rows] == ["text", "image", "video", "tts", "music"]
    for x in rows:
        assert "key_env" not in x and "notes" not in x
        assert x["health_state"] in {"ok", "missing", "unknown", "unconfigured",
                                     "unregistered"}


def _video_ready(c):
    rows = _rows(c)
    body = _payload(rows, video_local={
        "provider": "fastmetal", "model": "fastmetal-5b-qad", "enabled": True},
        video_cloud={"provider": "wan", "model": "wan-1080p", "enabled": True,
                     "is_default": False})
    r = c.put("/api/providers", json=body)
    assert r.status_code == 200, r.text


def test_解析默认走本地_本地坏了也不兜底云端(as_role, provider_state):
    c = as_role("admin")
    _video_ready(c)
    got = services.resolve_provider("video")
    assert got["backend"] == "local" and got["provider"] == "fastmetal"

    # 把本地档停用，再把云端档启用：解析器必须报错，不能自作主张转云端
    rows = _rows(c)
    body = _payload(rows, video_local={"enabled": False},
                    video_cloud={"enabled": True, "is_default": False})
    assert c.put("/api/providers", json=body).status_code == 200
    with pytest.raises(services.NoRoute) as exc:
        services.resolve_provider("video")
    assert "没有启用" in str(exc.value)


def test_云端视频要显式选择并确认(as_role, provider_state):
    c = as_role("admin")
    _video_ready(c)
    with pytest.raises(services.NoRoute) as exc:
        services.resolve_provider("video", "cloud")
    assert "确认" in str(exc.value)
    got = services.resolve_provider("video", "cloud", confirmed_cloud=True)
    assert got["backend"] == "cloud"


def test_解析拒绝未登记与空配置(as_role, provider_state):
    c = as_role("admin")
    rows = _rows(c)
    body = _payload(rows, tts_local={"provider": "skywalker", "model": "x", "enabled": True})
    assert c.put("/api/providers", json=body).status_code == 200
    with pytest.raises(services.NoRoute) as exc:
        services.resolve_provider("tts")
    assert "没有登记适配器" in str(exc.value)

    body = _payload(_rows(c), tts_local={"provider": "", "model": "", "enabled": False})
    assert c.put("/api/providers", json=body).status_code == 200
    with pytest.raises(services.NoRoute):
        services.resolve_provider("tts")
