"""W08：参数模板的边界。

模板只影响"下一次填表"，不影响已经提交的 Job；
参数必须过 adapter 白名单；凭据不能塞进模板；全局模板只有管理员能写。
"""


def _put_video(c, local=None):
    rows = c.get("/api/providers").json()
    body = []
    for r in rows:
        item = {k: r[k] for k in ("id", "provider", "model", "enabled", "is_default")}
        item["key_env"] = r["key_env_custom"]
        item["notes"] = r["notes"]
        if r["capability"] == "video" and r["backend"] == "local" and local:
            item.update(local)
        body.append(item)
    resp = c.put("/api/providers", json=body)
    assert resp.status_code == 200, resp.text


def _make(c, pid, **kw):
    body = {"project_id": pid, "name": "镜头默认", "capability": "video",
            "provider_code": "fastmetal", "model_code": "fastmetal-1.3b-qad",
            "params": {"width": 448, "height": 832}, "is_default": False}
    body.update(kw)
    return c.post("/api/generation-profiles", json=body)


def test_建模板_默认唯一_同名拒绝(as_role, provider_state, fresh_project):
    pid = fresh_project()
    c = as_role("editor")
    first = _make(c, pid, name="标准镜头", is_default=True)
    assert first.status_code == 201, first.text
    second = _make(c, pid, name="竖屏", params={"width": 448, "height": 832},
                   is_default=True)
    assert second.status_code == 201, second.text

    rows = c.get(f"/api/generation-profiles?project_id={pid}").json()
    defaults = [r for r in rows if r["is_default"]]
    assert len(defaults) == 1 and defaults[0]["name"] == "竖屏"

    dup = _make(c, pid, name="竖屏")
    assert dup.status_code == 409


def test_参数必须过适配器白名单_凭据不收(as_role, provider_state, fresh_project):
    pid = fresh_project()
    c = as_role("editor")

    bad = _make(c, pid, params={"bogus": 1})
    assert bad.status_code == 422 and "白名单" in bad.json()["detail"]

    misaligned = _make(c, pid, name="对齐", params={"width": 700})
    assert misaligned.status_code == 422 and "16 的倍数" in misaligned.json()["detail"]

    secret = _make(c, pid, name="带密钥", params={"api_key": "x"})
    assert secret.status_code == 422 and "凭据" in secret.json()["detail"]

    unknown = _make(c, pid, name="野 provider", provider_code="nope",
                    model_code="nope-1", params={})
    assert unknown.status_code == 422 and "适配器" in unknown.json()["detail"]

    unbound = _make(c, pid, name="脱钩", provider_code="", model_code="",
                    params={"width": 448})
    assert unbound.status_code == 422 and "不能带参数" in unbound.json()["detail"]


def test_改模板不影响已提交作业的快照(as_role, provider_state, clean_queue,
                                  fresh_project, register_tier):
    pid = fresh_project()
    _put_video(as_role("admin"), local={"provider": "fastmetal",
                                        "model": "fastmetal-1.3b-qad",
                                        "enabled": True})
    register_tier("video", "local", "fastmetal", "fastmetal-1.3b-qad")
    c = as_role("editor")
    profile = _make(c, pid, params={"width": 448, "height": 832}).json()

    job = c.post("/api/jobs", json={
        "project_id": pid, "capability": "video", "prompt": "冻结参数",
        "execution_mode": "local",
        "provider_code": "fastmetal", "model_code": "fastmetal-1.3b-qad",
        "params": {"width": 448, "height": 832},
    })
    assert job.status_code == 202, job.text

    changed = c.put(f"/api/generation-profiles/{profile['id']}", json={
        "name": profile["name"], "params": {"width": 512, "height": 832},
        "is_default": True})
    assert changed.status_code == 200, changed.text

    detail = c.get(f"/api/jobs/{job.json()['id']}").json()
    assert detail["params"] == {"width": 448, "height": 832}


def test_全局模板只有管理员能写_所有人能读(as_role, fresh_project):
    pid = fresh_project()
    editor = as_role("editor")
    denied = _make(editor, None, name="全局默认", provider_code="", model_code="",
                   params={})
    assert denied.status_code == 403

    admin = as_role("admin")
    made = _make(admin, None, name="全局默认", provider_code="", model_code="",
                 params={})
    assert made.status_code == 201, made.text
    assert made.json()["project_id"] is None

    rows = editor.get(f"/api/generation-profiles?project_id={pid}").json()
    assert any(r["id"] == made.json()["id"] for r in rows)

    edit = editor.put(f"/api/generation-profiles/{made.json()['id']}", json={
        "name": "改名", "params": {}, "is_default": False})
    assert edit.status_code == 403
    delete = editor.delete(f"/api/generation-profiles/{made.json()['id']}")
    assert delete.status_code == 403


def test_改删项目模板_别人看不见(as_role, provider_state, fresh_project):
    pid = fresh_project()
    owner = as_role("editor")
    made = _make(owner, pid).json()

    outsider = as_role("outsider")
    assert outsider.get(f"/api/generation-profiles?project_id={pid}").status_code == 404
    assert outsider.put(f"/api/generation-profiles/{made['id']}", json={
        "name": "越权", "params": {}, "is_default": False}).status_code == 404
    assert outsider.delete(f"/api/generation-profiles/{made['id']}").status_code == 404

    gone = owner.delete(f"/api/generation-profiles/{made['id']}")
    assert gone.status_code == 204
    left = owner.get(f"/api/generation-profiles?project_id={pid}").json()
    assert [r for r in left if r["project_id"] == pid] == []
