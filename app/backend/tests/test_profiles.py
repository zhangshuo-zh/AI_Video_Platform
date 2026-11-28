"""W08：参数模板的边界。

模板只影响"下一次填表"，不影响已经提交的 Job；
参数必须过 adapter 白名单；凭据不能塞进模板；全局模板只有管理员能写。
"""
import json

import pytest


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


def _make_comfyui(c, pid, params):
    return _make(c, pid, name="ComfyUI 工作流预设", capability="image",
                 provider_code="comfyui", model_code="comfyui-workflow", params=params)


def test_ComfyUI长工作流可创建更新并读回(as_role, fresh_project):
    pid = fresh_project()
    c = as_role("editor")
    workflow = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "画" * 2000}}}
    params = {"workflow_json": json.dumps(workflow, ensure_ascii=False), "steps": 20}
    assert 4000 < len(json.dumps(params, ensure_ascii=False).encode("utf-8")) <= 262144

    created = _make_comfyui(c, pid, params)
    assert created.status_code == 201, created.text
    profile = created.json()
    assert profile["params"] == params

    params["steps"] = 30
    updated = c.put(f"/api/generation-profiles/{profile['id']}", json={
        "name": profile["name"], "params": params,
    })
    assert updated.status_code == 200, updated.text
    rows = c.get("/api/generation-profiles", params={"project_id": pid}).json()
    assert next(row for row in rows if row["id"] == profile["id"])["params"] == params


@pytest.mark.parametrize("workflow_json", ["{", "[]", "null", " ", '{"1": NaN}', {"1": {}}])
def test_ComfyUI非法工作流不能保存(as_role, fresh_project, workflow_json):
    response = _make_comfyui(as_role("editor"), fresh_project(), {"workflow_json": workflow_json})
    assert response.status_code == 422
    assert "workflow_json" in response.json()["detail"]


@pytest.mark.parametrize("params", [
    {"workflow_json": "{}", "secret": "test"},
    {"workflow_json": '{"1": {"inputs": {"api_key": "test"}}}'},
], ids=["secret-in-params", "api-key-in-workflow"])
def test_ComfyUI工作流模板仍拦截凭据(as_role, fresh_project, params):
    response = _make_comfyui(as_role("editor"), fresh_project(), params)
    assert response.status_code == 422
    assert "不能放凭据" in response.json()["detail"]


@pytest.mark.parametrize("extra_params", [{}, {"workflow_json": ""}, {"workflow_json": None}])
def test_无工作流时模板仍限4000字节(as_role, fresh_project, extra_params):
    params = {"negative_prompt": "画" * 1400, **extra_params}
    response = _make_comfyui(as_role("editor"), fresh_project(), params)
    assert response.status_code == 422
    assert "上限 4KB" in response.json()["detail"]


def test_ComfyUI模板256KB上限按总参数字节计算(as_role, fresh_project):
    pid = fresh_project()
    c = as_role("editor")
    params = {"workflow_json": "{}", "negative_prompt": ""}
    remaining = 262144 - len(json.dumps(params, ensure_ascii=False).encode("utf-8"))
    params["workflow_json"] = " " * (remaining - 1) + "{}"
    params["negative_prompt"] = "x"
    assert _make_comfyui(c, pid, params).status_code == 201

    params["negative_prompt"] += "x"
    response = _make_comfyui(c, pid, params)
    assert response.status_code == 422
    assert "上限 256KB" in response.json()["detail"]


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
