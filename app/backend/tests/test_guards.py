"""43 条路由的守卫回归（W01-a 的主菜）。

为什么值得写成表驱动：守卫这件事最怕的是"新加一条端点忘了挂"。
手写一条条测试会在加端点时漏掉，而这张表 + `test_路由表没有偷偷变多`
那条断言会在漏挂时立刻红。

两个视角：
- **匿名**：除 `/api/health` 与四条 auth 之外，一律 401。
- **越权**：用 outsider（editor 角色，但不是那个项目的协作人）打每一条——
  项目级的东西必须 **404**（不可见，不透露存在），账号/系统级的东西必须 **403**
  （editor 没有 `user:*` / `system:config`），能看见的列表类必须 200。
"""
import re

import pytest

from db import get_conn

# (方法, 路径, 需要的最小 body, outsider 期望状态码)
# 路径里的 {p} {e} {s} {a} {u} {r} {d} {c} 由 fixture 填真实 id
ROUTES = [
    # ---------- 项目 ----------
    ("GET", "/api/projects", None, 200),
    ("POST", "/api/projects", {"name": "越权测试项目"}, 201),
    ("PUT", "/api/projects/{p}", {"name": "改名", "description": "", "tags": []}, 404),
    # editor 没有 project:delete ⇒ 先撞权限点那一层（403），轮不到"看不见"（404）。
    # 顺序是规格 02 §5.2 定的：会话 → 权限点 → 归属。
    ("DELETE", "/api/projects/{p}?confirm=x", None, 403),
    ("POST", "/api/projects/{p}/open", None, 404),
    ("PATCH", "/api/projects/{p}/status", {"status": "archived"}, 404),
    ("PUT", "/api/projects/{p}/tags", {"tags": ["t"]}, 404),
    ("GET", "/api/projects/{p}/summary", None, 404),
    ("POST", "/api/projects/batch", {"ids": ["{p}"], "action": "archive"}, 200),
    ("POST", "/api/projects/batch-delete", {"ids": ["{p}"], "confirm": "批量删除"}, 403),
    ("GET", "/api/projects/{p}/members", None, 404),
    ("POST", "/api/projects/{p}/members", {"user_id": 1}, 404),
    ("POST", "/api/projects/{p}/transfer", {"user_id": 1}, 404),
    ("DELETE", "/api/projects/{p}/members/{u}", None, 404),
    ("GET", "/api/member-candidates", None, 200),
    ("GET", "/api/tags", None, 200),
    # ---------- 创作单元 / 分镜 ----------
    ("GET", "/api/projects/{p}/episodes", None, 404),
    ("POST", "/api/projects/{p}/episodes", {"title": "第 9 集"}, 404),
    ("PATCH", "/api/episodes/{e}/status", {"status": "done"}, 404),
    ("DELETE", "/api/episodes/{e}", None, 404),
    ("GET", "/api/episodes/{e}/shots", None, 404),
    ("POST", "/api/episodes/{e}/shots", {"summary": "一镜"}, 404),
    ("DELETE", "/api/shots/{s}", None, 404),
    # ---------- 资产 ----------
    ("GET", "/api/projects/{p}/assets", None, 404),
    ("POST", "/api/projects/{p}/assets", {"kind": "character", "name": "越权角色"}, 404),
    ("GET", "/api/assets/{a}", None, 404),
    ("PUT", "/api/assets/{a}", {"kind": "character", "name": "改名"}, 404),
    ("PUT", "/api/assets/{a}/cover", {"path": "1/x.png"}, 404),
    ("GET", "/api/assets/{a}/shots", None, 404),
    ("DELETE", "/api/assets/{a}", None, 404),
    ("POST", "/api/assets/{a}/images", "FILE", 404),
    ("DELETE", "/api/assets/{a}/images?path=x", None, 404),
    # ---------- 文档（W05：元数据在库、正文在工作区、历史在 Git） ----------
    # 全部 404：文档一定挂项目，别人看不见这个项目时连"这份文档存在"都不该知道。
    # 这里没有 403 的余地——资产才有全局池那档特例（见 access.ensure_asset）。
    ("GET", "/api/projects/{p}/documents", None, 404),
    ("POST", "/api/projects/{p}/documents", {"kind": "novel", "title": "越权稿"}, 404),
    ("GET", "/api/documents/{d}", None, 404),
    ("PUT", "/api/documents/{d}", {"title": "改名"}, 404),
    ("PATCH", "/api/documents/{d}/status", {"status": "polishing"}, 404),
    ("DELETE", "/api/documents/{d}", None, 404),
    ("GET", "/api/documents/{d}/content", None, 404),
    ("PUT", "/api/documents/{d}/content",
     {"content": "抢写", "expected_version": "sha256:不存在的那个版本"}, 404),
    ("POST", "/api/documents/{d}/lock", None, 404),
    ("DELETE", "/api/documents/{d}/lock", None, 404),
    ("GET", "/api/documents/{d}/versions", None, 404),
    ("GET", "/api/documents/{d}/versions/{c}/diff", None, 404),
    # ---------- 账号与角色（editor 一律 403） ----------
    ("GET", "/api/users", None, 403),
    ("POST", "/api/users", {"username": "zz_probe", "display_name": "", "password": "123456"}, 403),
    ("PATCH", "/api/users/{u}", {"display_name": "改"}, 403),
    ("PUT", "/api/users/{u}/roles", {"roles": ["viewer"]}, 403),
    ("POST", "/api/users/{u}/password", {"new_password": "abcdef"}, 403),
    ("DELETE", "/api/users/{u}", {"transfer_to": None, "delete_projects": False}, 403),
    ("GET", "/api/roles", None, 403),
    ("GET", "/api/permissions", None, 403),
    ("PUT", "/api/roles/{r}/permissions", {"permissions": []}, 403),
    # ---------- 生成作业（W07） ----------
    # 作业一定挂项目：看不见项目就一律 404（连"这条作业存在"都不透露）
    ("POST", "/api/jobs", {"project_id": "{p}", "capability": "video",
                           "prompt": "越权作业", "provider_code": "mock",
                           "model_code": "mock-1", "params": {}}, 404),
    ("GET", "/api/jobs", None, 200),
    ("GET", "/api/jobs/{j}", None, 404),
    ("POST", "/api/jobs/{j}/cancel", None, 404),
    ("POST", "/api/jobs/{j}/retry", None, 404),
    ("GET", "/api/jobs/{j}/logs", None, 404),
    ("GET", "/api/jobs/{j}/artifacts", None, 404),
    ("GET", "/api/jobs/{j}/artifacts/{i}", None, 404),
    # 成本账按可见项目汇总：看不见那个项目时就是空账，不是 404
    ("GET", "/api/jobs/costs", None, 200),
    ("GET", "/api/worker/status", None, 200),
    # ---------- 参数模板（W08） ----------
    ("GET", "/api/generation-profiles", None, 200),
    ("POST", "/api/generation-profiles", {"project_id": "{p}", "name": "越权模板",
                                          "capability": "video"}, 404),
    # 全局模板只有管理员能建：editor 的 job:submit 过了，也在这里被挡
    ("POST", "/api/generation-profiles", {"name": "全局越权", "capability": "video"}, 403),
    ("PUT", "/api/generation-profiles/{g}", {"name": "改名", "params": {},
                                             "is_default": False}, 404),
    ("DELETE", "/api/generation-profiles/{g}", None, 404),
    # ---------- Provider（editor 没有 system:config） ----------
    ("GET", "/api/providers", None, 403),
    ("GET", "/api/providers/manifest", None, 403),
    # 当前默认是给普通模块看的只读摘要：有会话就能读，不带密钥/备注
    ("GET", "/api/providers/current", None, 200),
    # 提交表单要用：只回可用的槽位与参数白名单，不带密钥变量名/备注
    ("GET", "/api/providers/available", None, 200),
    ("PUT", "/api/providers", [{"id": 1, "provider": "x", "model": "",
                                "enabled": False, "is_default": True}], 403),
]

# auth 四条与 health 不参与"匿名 401"这张表，单独测
EXCLUDED = {"/api/health", "/api/auth/login", "/api/auth/logout",
            "/api/auth/me", "/api/auth/password"}


def _fill(path: str, ids: dict) -> str:
    out = path
    for k, v in ids.items():
        out = out.replace("{" + k + "}", str(v))
    return out


@pytest.fixture
def ids(seeded, people):
    """表里那些占位符对应的真实 id。角色 id 现查，不写死 1/2/3。"""
    with get_conn() as conn:
        role_id = conn.execute("SELECT id FROM role WHERE code = 'viewer'").fetchone()["id"]
    return {"p": seeded["project_id"], "e": seeded["episode_id"], "s": seeded["shot_id"],
            "a": seeded["asset_id"], "u": people["viewer"]["id"], "r": role_id,
            "d": seeded["document_id"], "c": seeded["commit"],
            "j": seeded["job_id"], "g": seeded["profile_id"], "i": 0}


def _sub(v, ids):
    """递归替换 body 里的 "{p}" 这类占位符——ids 是数组的接口（batch）也要能填。"""
    if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
        return ids[v[1:-1]]
    if isinstance(v, list):
        return [_sub(x, ids) for x in v]
    if isinstance(v, dict):
        return {k: _sub(x, ids) for k, x in v.items()}
    return v


def _send(client, method, path, body, ids):
    url = _fill(path, ids)
    if body == "FILE":                      # 上传接口要 multipart，给一张最小合法 PNG
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                            "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")
        return client.request(method, url, files={"file": ("x.png", png, "image/png")})
    return client.request(method, url, json=_sub(body, ids) if body is not None else None)


@pytest.mark.parametrize("method,path,body,_expect", ROUTES,
                         ids=[f"{m} {p}" for m, p, _, _ in ROUTES])
def test_匿名一律_401(client, ids, method, path, body, _expect):
    """没登录时，所有业务端点都必须 401——不例外于"看起来无害"的 GET。"""
    r = _send(client, method, path, body, ids)
    assert r.status_code == 401, f"{method} {path} → {r.status_code}（应为 401）"


@pytest.mark.parametrize("method,path,body,expect", ROUTES,
                         ids=[f"{m} {p}" for m, p, _, _ in ROUTES])
def test_越权一律被拒(as_role, ids, method, path, body, expect):
    """editor 打别人的项目要 404（不透露存在），打账号/系统要 403。"""
    c = as_role("outsider")
    r = _send(c, method, path, body, ids)
    assert r.status_code == expect, (
        f"{method} {path} → {r.status_code}（期望 {expect}）｜{r.text[:120]}")


def test_批量接口把看不见的收进_skipped_而不是整批失败(as_role, seeded):
    """规格 01-2.2-R2：批量不得因一项失败而放宽权限，也不必要地全批回滚。"""
    c = as_role("editor")          # editor 看得见自己的项目
    r = c.post("/api/projects/batch", json={"ids": [seeded["project_id"], 999999],
                                            "action": "archive"})
    assert r.status_code == 200
    body = r.json()
    assert seeded["project_id"] in body["done"]
    assert any(s["id"] == 999999 and s["reason"] for s in body["skipped"]), body


def test_路由表与本表条数对齐(app):
    """表驱动测试的守门人：加端点必须同时进这张表，否则这里先红。

    不写死"43 条"这种数字——写死的人总会忘记改，忘了改就等于新端点
    可以悄悄不进守卫回归。真正起作用的是下面那条集合相等。
    """
    paths = app.openapi()["paths"]
    real = {(m.upper(), p) for p, ops in paths.items() for m in ops}
    business = {(m, p) for m, p in real if p not in EXCLUDED}
    tabled = {(m, p.split("?")[0]) for m, p, _, _ in ROUTES}
    # 占位符名字两边不一样（表里写 {p}，openapi 里是 {project_id}），归一后再比
    norm = lambda s: re.sub(r"\{[^}]+\}", "{}", s)
    a, b = {(m, norm(p)) for m, p in business}, {(m, norm(p)) for m, p in tabled}
    assert a == b, f"表里缺：{sorted(a - b)}；表里多：{sorted(b - a)}"
