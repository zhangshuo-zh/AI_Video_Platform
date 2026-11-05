"""模型中心（0035）：登记层、能力标签、状态口径、任务调用策略。

要守住的三句话，逐条钉死：

1. **登记不是清单**。规格（参数、计价、出处）永远回 `adapters.py` 查，
   `model_registration` 只存"这台机器上的实例事实"。清单里没有的 provider
   允许登记，但必须明说"参数不猜"，且提交侧照旧拒。
2. **三种状态不许混为一谈**。登记状态、引擎连接状态、实际可用状态是三件事：
   引擎在线不代表模型可用，模型文件在也不代表工作流依赖齐，
   而"可用"必须同时满足 已启用 ∧ 健康就绪 ∧ 这一档自己有过真实调用或产物验收。
3. **策略只改配置**。保存策略不得启动任何任务、不得改默认轨、
   不得让备用轨自动顶上（裁定 MC1＝只提示不自动切）。

用例不打真供应商。本地"可用"那一档靠把 `FASTVIDEO_FASTMETAL_HOME`
指到一个自己搭的假目录树（解释器/入口脚本/权重五件套齐），
验的是 `runtime.probe` 真的按文件说话，不是按模型名说话。
"""
import json
from pathlib import Path
import os
import uuid

import pytest

import jobfiles
import model_center as mc
import validations
from repositories.jobs import jobs as job_repo
from repositories.model_center import RegistrationError, model_center
from repositories.providers import providers as provider_repo
from repositories.validation import validations as val_repo


# ---------------------------------------------------------------- 建数据

def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def _fake_fastmetal_home(tmp_path):
    """搭一个能让 runtime.probe_fastmetal 说 ok 的目录树。

    缺哪一件 probe 就会点名哪一件——所以这里必须五件套全搭，
    而不是 monkeypatch health() 假装就绪。
    """
    home = tmp_path / "FastVideo"
    _touch(home / ".venv" / "bin" / "python")
    _touch(home / "examples" / "inference" / "basic" / "mlx_wan_prompt_to_video.py")
    model = home / "FastMetal-1.3B-QAD"
    for name in ("mlx_dit.json", "mlx_dit.safetensors"):
        _touch(model / name)
    for name in ("text_encoder", "tokenizer", "vae"):
        (model / name).mkdir(parents=True, exist_ok=True)
    # 不写 os.environ：环境变量一律由用例的 monkeypatch 设，测试跑完自动收回去
    return home


@pytest.fixture
def clean_registrations(app):
    """登记行是全局的：动过就清干净，免得后面的用例读到上一个用例的行。"""
    from db import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM model_registration")
    yield
    with get_conn() as conn:
        conn.execute("DELETE FROM model_registration")


@pytest.fixture
def as_admin(app, people):
    from fastapi.testclient import TestClient
    return TestClient(app, cookies={"fv_session": people["admin"]["token"]})


def _item_of(items, provider, model_code):
    for it in items:
        if it["provider"] == provider and it["model_code"] == model_code:
            return it
    raise AssertionError(f"目录里没有 {provider}/{model_code}")


def _succeeded_cloud_job(pid, provider="wan", model="wan2.7-t2v"):
    """一条真跑过、产物在盘上、且带计费确认的云端作业——真实调用凭据的唯一合法来源。"""
    row = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_cloud",
        "execution_mode": "cloud", "provider_code": provider, "model_code": model,
        "adapter_version": "test", "prompt_snapshot": "模型中心用", "params": {},
        "input_refs": [], "user_confirmed_cloud": True,
    })
    claimed = job_repo.claim_next("mc-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    job_repo.succeed(row["id"], "mc-worker", "", [jobfiles.relative(target)], 1,
                     provider_request_id="req-mc-1")
    return job_repo.get(row["id"])


def _succeeded_local_job(pid, provider="fastmetal", model="fastmetal-1.3b-qad"):
    """本地版的成功作业：不需要计费确认，其余与云端那条同构。"""
    row = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": provider, "model_code": model,
        "adapter_version": "test", "prompt_snapshot": "模型中心用", "params": {},
        "input_refs": [],
    })
    claimed = job_repo.claim_next("mc-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    job_repo.succeed(row["id"], "mc-worker", "", [jobfiles.relative(target)], 1)
    return job_repo.get(row["id"])


# ---------------------------------------------------------------- 登记层

def test_迁移建了两张表并给九个场景立了骨架(as_admin):
    r = as_admin.get("/api/model-policies")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 9, "任务场景骨架必须来自 0035 的种子，不在前端写死"
    # 骨架一律 enabled=0：没配建议轨的策略不该显示成生效
    assert all(not it["enabled"] for it in body["items"])
    assert body["no_auto_fallback"] is True


def test_清单里有但没登记的档必须出现在目录里(as_admin, clean_registrations):
    """少了这一段，"装了不等于配了"就看不见——这是仓库页存在的理由之一。"""
    items = as_admin.get("/api/models").json()["items"]
    unregistered = [i for i in items if i["status"] == "not_registered"]
    # 全新库里一条登记都没有：清单的 9 档全部应当是"未登记"
    assert len(unregistered) == 9
    assert all(not i["registered"] for i in unregistered)


def test_新增登记后目录里那一行变成已登记(as_admin, clean_registrations):
    r = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad", "display_name": "预览用的 1.3B",
        "artifact_path": "~/FastVideo/FastMetal-1.3B-QAD"})
    assert r.status_code == 201, r.text
    assert r.json()["in_manifest"] is True
    item = _item_of(as_admin.get("/api/models").json()["items"],
                    "fastmetal", "fastmetal-1.3b-qad")
    assert item["registered"] is True
    assert item["display_name"] == "预览用的 1.3B"


def test_登记不传source时运行位置跟随backend(as_admin, clean_registrations):
    """界面只传 backend；source 是"运行位置"的人话口径，不能默认成本地。

    默认写死 "local" 会让云端登记在仓库页显示成"本地"——用户选的与看到的
    是两个位置。这里钉住：不传 source 就照 backend 走，云端就是云端。
    """
    assert as_admin.post("/api/models", json={
        "capability": "image", "backend": "cloud", "provider": "sdxl-api",
        "model_code": "sdxl-source"}).status_code == 201
    item = _item_of(as_admin.get("/api/models").json()["items"],
                    "sdxl-api", "sdxl-source")
    assert item["backend"] == "cloud"
    assert item["source"] == "cloud"


def test_同一档重复登记被拒而不是静默覆盖(as_admin, clean_registrations):
    body = {"capability": "video", "backend": "local", "provider": "fastmetal",
            "model_code": "fastmetal-5b-qad"}
    assert as_admin.post("/api/models", json=body).status_code == 201
    r = as_admin.post("/api/models", json={**body, "display_name": "第二次"})
    assert r.status_code == 422
    assert "已经登记过了" in r.json()["detail"]


def test_清单里没有的provider允许登记但明说参数不猜(as_admin, clean_registrations):
    """先登记、下周再补规格"是真实工作流；硬挡只会逼人填一个假名字进清单。"""
    r = as_admin.post("/api/models", json={
        "capability": "image", "backend": "cloud", "provider": "sdxl-api",
        "model_code": "sdxl-1", "remote_model_id": "sdxl-v1"})
    assert r.status_code == 201
    assert r.json()["in_manifest"] is False
    assert "参数不猜" in r.json()["warning"]
    item = _item_of(as_admin.get("/api/models").json()["items"], "sdxl-api", "sdxl-1")
    assert item["status"] == "unregistered"
    assert item["params"] == [] and item["prices"] == []


def test_撤登记必须带confirm且不碰文件与历史(as_admin, clean_registrations, fresh_project):
    """移除登记 ≠ 删除模型。这句话必须在接口上就成立，不能只写在界面文案里。"""
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad"}).json()["id"]
    job = _succeeded_cloud_job(fresh_project())
    assert as_admin.delete(f"/api/models/{rid}").status_code == 422
    assert as_admin.delete(f"/api/models/{rid}?confirm=撤登记").status_code == 200
    assert model_center.registration(rid) is None
    # 历史作业与验证记录一条没动
    assert job_repo.get(job["id"]) is not None
    assert val_repo.list_recent(200) is not None


def test_禁用只改状态不删任何东西(as_admin, clean_registrations):
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad"}).json()["id"]
    as_admin.patch(f"/api/models/{rid}", json={"enabled": False})
    item = _item_of(as_admin.get("/api/models").json()["items"],
                    "fastmetal", "fastmetal-1.3b-qad")
    assert item["status"] == "disabled"
    assert item["enabled"] is False
    assert model_center.registration(rid) is not None


def test_改字段只动传上来的那几个(as_admin, clean_registrations):
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad", "display_name": "原名",
        "note": "原来的备注"}).json()["id"]
    out = as_admin.patch(f"/api/models/{rid}", json={"display_name": "新名"}).json()["item"]
    assert out["display_name"] == "新名"
    assert out["note"] == "原来的备注"


def test_登记不认识的字段直接被拒(as_admin, clean_registrations):
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad"}).json()["id"]
    # 走仓储层直接塞一个野字段：路由层的 Pydantic 已经挡掉未知键，
    # 这一条守的是"以后有人绕过路由调仓储也不会写进一个没人认识的列"
    with pytest.raises(RegistrationError):
        model_center.update(rid, {"params_json": "{}"})


# ---------------------------------------------------------------- 能力标签

def test_标签只从参数表数出来不从模型名猜(as_admin, clean_registrations):
    """`wan2.2-i2v-a14b` 被标成"图生视频"是因为它登记了一个 type=="image" 的参数，
    不是因为名字里有 i2v。反过来 flux1-schnell 没有图片输入参数，就拿不到"图生图"。"""
    for provider, code in (("mlx-video", "wan2.2-i2v-a14b"),
                           ("mlx-video", "wan2.2-t2v-a14b"),
                           ("mflux", "flux1-schnell")):
        as_admin.post("/api/models", json={"capability": _cap_of(provider),
                                           "backend": "local", "provider": provider,
                                           "model_code": code})
    items = as_admin.get("/api/models").json()["items"]
    i2v = {t["tag"] for t in _item_of(items, "mlx-video", "wan2.2-i2v-a14b")["tags"]}
    t2v = {t["tag"] for t in _item_of(items, "mlx-video", "wan2.2-t2v-a14b")["tags"]}
    flux = {t["tag"] for t in _item_of(items, "mflux", "flux1-schnell")["tags"]}
    assert {"文生视频", "参考图", "图生视频"} <= i2v
    assert "参考图" not in t2v and "图生视频" not in t2v
    assert "图生图" not in flux and "参考图" not in flux


def test_人工声明未核验时不算已验证(as_admin, clean_registrations):
    rid = as_admin.post("/api/models", json={
        "capability": "image", "backend": "local", "provider": "mflux",
        "model_code": "flux1-schnell", "capabilities": ["角色一致性"]}).json()["id"]
    item = _item_of(as_admin.get("/api/models").json()["items"], "mflux", "flux1-schnell")
    manual = [t for t in item["tags"] if t["tag"] == "角色一致性"]
    assert manual and manual[0]["source"] == "manual"
    assert manual[0]["verified"] is False


def test_真调用与产物验收之后人工声明才升级(as_admin, clean_registrations, fresh_project,
                                            provider_state, monkeypatch):
    """验证记录只到 provider 粒度，所以还要求"槽位当前用的就是这一档"，
    换档不许拿旧记录背书。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "not-a-real-key")
    provider_repo.replace_many([{
        "id": provider_repo.slot("video", "cloud")["id"], "provider": "wan",
        "model": "wan2.7-t2v", "enabled": True, "is_default": False,
        "key_env": "", "notes": ""}])
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "cloud", "provider": "wan",
        "model_code": "wan2.7-t2v", "capabilities": ["首尾帧控制"]}).json()["id"]
    job = _succeeded_cloud_job(fresh_project())
    validations.from_job(job["id"], "real_call", None, "模型中心用例")
    validations.from_job(job["id"], "artifact_review", None, "看过产物", passed="pass")

    item = _item_of(as_admin.get("/api/models").json()["items"], "wan", "wan2.7-t2v")
    manual = [t for t in item["tags"] if t["tag"] == "首尾帧控制"][0]
    assert manual["verified"] is True
    assert item["status"] == "available"
    assert item["health"]["state"] == "ok"


def test_换档之后旧验证记录不背书(as_admin, clean_registrations, fresh_project,
                                 provider_state, monkeypatch, tmp_path):
    """验证记录只到 provider 粒度（`model_validation` 按 capability×backend×provider 索引），
    所以槽位当前用的是哪一档必须再比一次：1.3B 验过，不等于 5B 验过。"""
    home = _fake_fastmetal_home(tmp_path)
    # 把 5B 那一档也搭齐：两个入口脚本、两套权重，健康才能都说 ok
    _touch(home / "examples" / "inference" / "basic" / "mlx_wan22_generate.py")
    model5 = home / "FastMetal-5B-QAD"
    for name in ("mlx_dit.json", "mlx_dit.safetensors"):
        _touch(model5 / name)
    for name in ("text_encoder", "tokenizer", "vae"):
        (model5 / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))

    slot = provider_repo.slot("video", "local")
    provider_repo.replace_many([{
        "id": slot["id"], "provider": "fastmetal", "model": "fastmetal-1.3b-qad",
        "enabled": True, "is_default": True, "key_env": "", "notes": ""}])
    for code in ("fastmetal-1.3b-qad", "fastmetal-5b-qad"):
        as_admin.post("/api/models", json={"capability": "video", "backend": "local",
                                           "provider": "fastmetal", "model_code": code})
    job = _succeeded_local_job(fresh_project(), model="fastmetal-1.3b-qad")
    validations.from_job(job["id"], "real_call", None, "验的是 1.3B")
    validations.from_job(job["id"], "artifact_review", None, "看过", passed="pass")

    items = as_admin.get("/api/models").json()["items"]
    assert _item_of(items, "fastmetal", "fastmetal-1.3b-qad")["status"] == "available"
    assert _item_of(items, "fastmetal", "fastmetal-5b-qad")["status"] == "unverified", \
        "同一 provider 的旧记录不能替另一档说「已验证」"


def test_本地模型健康就绪才算可用缺一件就点名(as_admin, clean_registrations, tmp_path,
                                            provider_state, monkeypatch):
    """把 HOME 指到一个真的搭齐了目录树的位置——验的是 runtime.probe 按文件说话。"""
    home = _fake_fastmetal_home(tmp_path)
    monkeypatch.setenv("FASTVIDEO_FASTMETAL_HOME", str(home))
    as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad", "artifact_path": str(home / "FastMetal-1.3B-QAD")})
    item = _item_of(as_admin.get("/api/models").json()["items"],
                    "fastmetal", "fastmetal-1.3b-qad")
    assert item["health"]["state"] == "ok", item["health"]["detail"]
    # 环境齐了也还没验过：可用要求真实调用或产物验收，缺东西那一档才是 missing
    assert item["status"] == "unverified"
    # 抽掉权重目录里的一件，健康必须点名是哪一件
    (home / "FastMetal-1.3B-QAD" / "mlx_dit.safetensors").unlink()
    item = _item_of(as_admin.get("/api/models").json()["items"],
                    "fastmetal", "fastmetal-1.3b-qad")
    assert item["health"]["state"] == "missing"
    assert "mlx_dit.safetensors" in item["health"]["detail"]
    assert item["status"] == "dependency_missing"


def test_远程自建服务本轮不许留验证痕(as_admin, clean_registrations):
    rid = as_admin.post("/api/models", json={
        "capability": "image", "backend": "remote", "provider": "sdxl-api",
        "model_code": "sdxl-1"}).json()["id"]
    r = as_admin.post(f"/api/models/{rid}/check")
    assert r.status_code == 422
    assert "只认 local/cloud" in r.json()["detail"]


# ---------------------------------------------------------------- 策略与路由

def test_保存策略不启动任何任务(as_admin, fresh_project):
    """任务书 §七 规则 8：保存只改配置。这一条必须用 job 计数钉死。"""
    from db import get_conn

    with get_conn() as conn:
        before = conn.execute("SELECT count(*) c FROM job").fetchone()["c"]
    r = as_admin.put("/api/model-policies", json=[{
        "scene_key": "character_base", "scene_label": "人物基础形象生成",
        "capability": "image", "required": ["文生图"],
        "slot_provider": "mflux", "slot_model": "flux1-schnell",
        "preference": "local", "enabled": True}])
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 1
    with get_conn() as conn:
        after = conn.execute("SELECT count(*) c FROM job").fetchone()["c"]
    assert after == before, "保存策略把作业建出来了——策略层不许碰执行"


def test_策略校验一次说清所有问题(as_admin):
    r = as_admin.put("/api/model-policies", json=[
        {"scene_key": "不存在的场景", "capability": "text"},
        {"scene_key": "scene_prompt", "capability": "text", "preference": "随便"},
        {"scene_key": "storyboard", "capability": "text", "enabled": True},
        {"scene_key": "prop_concept", "capability": "image",
         "slot_provider": "不存在的provider", "slot_model": "x"},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    for frag in ("没有这个任务场景", "偏好只能是", "要启用就得选一条建议轨", "不在清单里"):
        assert frag in detail, f"{frag} 没出现在一次性报错里：{detail}"


def test_备用轨与建议轨同档被拒(as_admin):
    r = as_admin.put("/api/model-policies", json=[{
        "scene_key": "shot_video", "capability": "video",
        "slot_provider": "fastmetal", "slot_model": "fastmetal-1.3b-qad",
        "fallback_provider": "fastmetal", "fallback_model": "fastmetal-1.3b-qad",
        "enabled": True}])
    assert r.status_code == 422
    assert "等于没有备用" in r.json()["detail"]


def test_场景候选只收能力齐的(as_admin, clean_registrations):
    """人物服装形态要求 图生图 + 参考图；mflux 不收参考图，必须被挡在候选外。"""
    as_admin.post("/api/models", json={"capability": "image", "backend": "local",
                                       "provider": "mflux", "model_code": "flux1-schnell"})
    as_admin.post("/api/models", json={"capability": "image", "backend": "local",
                                       "provider": "mflux", "model_code": "flux1-dev",
                                       "capabilities": ["图生图", "参考图"]})
    as_admin.put("/api/model-policies", json=[{
        "scene_key": "character_variant", "capability": "image",
        "required": ["图生图", "参考图"]}])
    out = as_admin.get("/api/model-policies/resolve/character_variant").json()
    assert out["required"] == ["图生图", "参考图"]
    assert out["candidates"] == [], "两条都不合格时不该给出任何候选"
    schnell = [b for b in out["blockers"] if "flux1-schnell" in b]
    assert schnell, f"缺能力的必须点名：{out['blockers']}"
    assert "缺能力" in schnell[0] and "图生图" in schnell[0]
    # 声明了能力但清单里没有的那条：点名"清单未登记"，而不是假装它不存在
    assert any("flux1-dev" in b and "清单未登记" in b for b in out["blockers"]), out["blockers"]


def test_候选接口明写不自动切换(as_admin):
    out = as_admin.get("/api/model-policies/resolve/scene_prompt").json()
    assert out["auto_switch"] is False
    assert "不会自动顶上" in out["policy_note"]


def test_未知场景不报错但说清没有(as_admin):
    out = as_admin.get("/api/model-policies/resolve/no_such_scene").json()
    assert out["found"] is False and out["candidates"] == []


# ---------------------------------------------------------------- 口径与脱敏

def test_顶部四数与目录同一口径(as_admin, clean_registrations):
    """任务书 §四：模型数量、可用数量、引擎数量必须口径一致。
    这里直接拿目录重算一遍，和 stats 对表——不一致就是两处各算了一套。"""
    as_admin.post("/api/models", json={"capability": "video", "backend": "local",
                                       "provider": "fastmetal",
                                       "model_code": "fastmetal-1.3b-qad"})
    items = as_admin.get("/api/models").json()["items"]
    s = as_admin.get("/api/models/stats").json()
    assert s["registered_total"] == len([i for i in items if i["registered"]])
    assert s["available_total"] == len([i for i in items if i["status"] == "available"])
    assert s["needs_attention"] == s["registered_total"] - s["available_total"]
    assert s["engine_total"] == len({i["engine_code"] for i in items if i["engine_code"]})
    assert "model_registration 的行数" in s["caliber"]["registered_total"]


def test_未接入引擎单独列出不计入引擎数(as_admin, clean_registrations):
    """ComfyUI 与 Draw Things 本机装了但平台零引用：
    它们必须出现在页面上，但不能混进"引擎总数"，更不能有一个能点的连接按钮。"""
    body = as_admin.get("/api/models").json()
    s = as_admin.get("/api/models/stats").json()
    codes = {e["code"] for e in body["unintegrated_engines"]}
    assert {"comfyui", "draw-things"} <= codes
    assert codes.isdisjoint({i["engine_code"] for i in body["items"]})
    assert codes.isdisjoint({i["provider"] for i in body["items"]})
    for e in s["unintegrated_engines"]:
        assert "未接入" in e["note"]


def test_响应里不出现任何密钥值(as_admin, clean_registrations, monkeypatch):
    """密钥值只从进程环境读，界面与日志都不许拿到它。"""
    secret = "sk-secret-" + uuid.uuid4().hex
    monkeypatch.setenv("DASHSCOPE_API_KEY", secret)
    as_admin.post("/api/models", json={"capability": "video", "backend": "cloud",
                                       "provider": "wan", "model_code": "wan2.7-t2v"})
    blob = json.dumps(as_admin.get("/api/models").json(), ensure_ascii=False)
    blob += json.dumps(as_admin.get("/api/models/stats").json(), ensure_ascii=False)
    assert secret not in blob
    # 变量名可以出现（那是配置的一部分），值不行
    assert "DASHSCOPE_API_KEY" in blob


def _cap_of(provider: str) -> str:
    """测试里按 provider 反查能力，避免把能力轴写死后和清单漂移。"""
    import adapters

    for a in adapters.ADAPTERS:
        if a.code == provider:
            return a.capability
    raise AssertionError(f"清单里没有 {provider}")


def test_枚举的说法只有一份出处(as_admin):
    """运行位置／策略开关都从后端拿，界面与路由都不许再抄一份字面量。

    三处原来是各抄各的：路由把 `("local","cloud","remote")` 写了三遍，
    `/model-policies` 的响应把 `no_auto_fallback: True` 手抄成字面量，
    前端 `models.js` 又硬写三个 `<option>`（标签还和后端差一个字：
    "远程自建服务" vs "远程自建"）。抄的东西不会报错，只会在改的时候漏一处。
    """
    import adapters
    from models import BACKENDS, CAPABILITIES, REG_SOURCES

    body = as_admin.get("/api/models").json()
    assert body["reg_sources"] == REG_SOURCES, "登记层的取值没跟着 models.REG_SOURCES"
    assert body["backends"] == BACKENDS and body["capabilities"] == CAPABILITIES
    # 选型层只有两档、登记层有三档：两把一起下发，界面才知道哪一栏该用哪一把
    assert set(BACKENDS) < set(REG_SOURCES)

    policies = as_admin.get("/api/model-policies").json()
    assert policies["no_auto_fallback"] is adapters.POLICY["no_auto_fallback"]
    # 权威在 adapters 那一份，不是路由里写死的 True：谁改了清单策略，
    # 这一句必须跟着动，否则界面会替平台说一句已经不成立的话。
    src = (Path(__file__).resolve().parents[1] / "routers" / "model_center.py").read_text()
    assert 'no_auto_fallback": adapters.POLICY["no_auto_fallback"]' in src
    assert '"no_auto_fallback": True' not in src, "路由里又出现了一份字面量 True"
    # 运行位置的白名单也不许在这里再数一遍
    assert '("local", "cloud", "remote")' not in src


def test_worker状态给得出在线数与历史数(as_role):
    """面板默认只列在线进程，"另有几个离线"这句话必须由服务端数。

    钉两件事：① `live_count`/`history_count` 与 `workers` 里的 online 标志一致
    （两处各算迟早分家）；② `workers` 仍然原样带着全部行——折叠是视图的事，
    把历史从响应里删掉就是另一回事了，那样"点开看"就没有数据来源。
    """
    import services
    from repositories.jobs import jobs as job_repo

    # 现场造一份：一个在线（心跳现在）、两个离线（3 天前、8 天前）
    job_repo.heartbeat("test-host:1:aaaaaa", 1, None, "test")
    job_repo.heartbeat("test-host:2:bbbbbb", 2, None, "test")
    job_repo.heartbeat("test-host:3:cccccc", 3, None, "test")
    with __import__("db").get_conn() as conn:
        conn.execute("UPDATE worker_node SET heartbeat_at = "
                     "datetime('now','localtime','-3 days') WHERE worker_id = ?",
                     ("test-host:2:bbbbbb",))
        conn.execute("UPDATE worker_node SET heartbeat_at = "
                     "datetime('now','localtime','-8 days') WHERE worker_id = ?",
                     ("test-host:3:cccccc",))
    view = services.worker_status()
    # 必须再走一次 HTTP：服务层算了但没进 `WorkerStatusOut` 的字段会被静默丢掉，
    # 只调函数的用例对那种事完全无感，界面拿到的却是一个都不剩。
    http = as_role("admin").get("/api/worker/status").json()
    for key in ("workers", "live_count", "history_count"):
        assert key in http, f"响应里没有 {key}（多半是 WorkerStatusOut 少声明了）"
    assert http["live_count"] == view["live_count"]
    assert http["history_count"] == view["history_count"]
    nodes = {n["worker_id"]: n for n in view["workers"]}
    assert nodes["test-host:1:aaaaaa"]["online"] is True
    assert nodes["test-host:2:bbbbbb"]["online"] is False
    # 8 天前那一行现在还在（清理只在下一次写心跳时顺带做），但它必须是离线：
    # 这条断言要能失败——写成"不在或离线"就等于永远为真，测不出任何东西
    assert nodes["test-host:3:cccccc"]["online"] is False
    live = sum(1 for n in view["workers"] if n["online"])
    assert view["live_count"] == live
    assert view["history_count"] == len(view["workers"]) - live
    assert view["history_count"] >= 1        # 这一条要真能看见"有历史"，不是永远 0
