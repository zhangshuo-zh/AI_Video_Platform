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
import uuid

import pytest

import adapters
import jobfiles
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
    unregistered = [i for i in items if i["status"] == "manifest_only"]
    # 数量从清单现数，不写死：W-B 加 draw_things 那一档时这里从 9 变 10，
    # 写死的版本只会以"红一次"的方式提醒人，而现数的版本根本不会红。
    expected = sum(len(adapter.models or ()) for adapter in adapters.ADAPTERS)
    assert expected >= 9, "清单至少要有的档数明显不对，先看 adapters.py 是不是被改坏"
    assert len(unregistered) == expected, "清单里有但没登记的档必须全部出现在目录里"
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
    assert item["status"] == "registration_only"
    assert item["params"] == [] and item["prices"] == []


def test_撤登记必须带confirm且不碰文件与历史(as_admin, clean_registrations, fresh_project):
    """移除登记 ≠ 删除模型。这句话必须在接口上就成立，不能只写在界面文案里。"""
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad"}).json()["id"]
    job = _succeeded_cloud_job(fresh_project())
    assert as_admin.delete(f"/api/models/{rid}").status_code == 422
    assert as_admin.delete(f"/api/models/{rid}?confirm=1").status_code == 200
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
    created = as_admin.post("/api/models", json={
        "capability": "image", "backend": "local", "provider": "mflux",
        "model_code": "flux1-schnell", "capabilities": ["角色一致性"]})
    assert created.status_code == 201, created.text
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
    created = as_admin.post("/api/models", json={
        "capability": "video", "backend": "cloud", "provider": "wan",
        "model_code": "wan2.7-t2v", "capabilities": ["首尾帧控制"]})
    assert created.status_code == 201, created.text
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
    other = _item_of(items, "fastmetal", "fastmetal-5b-qad")
    assert other["status"] == "unverified", \
        "同一 provider 的旧记录不能替另一档说「已验证」"
    # 「未验证」必须说出为什么别的路都走不通：不是环境问题，是它不是当前那一档。
    # 不点名这句话，人就会在这一行上反复点「环境检查」（那正是审查里最贵的一类困惑）。
    assert "不是能力轨当前选中的那一档" in other["status_detail"], other["status_detail"]
    assert "fastmetal-1.3b-qad" in other["status_detail"]
    # 槽位那一档是「可用」，它的说明仍然是健康原话，不许被这句盖掉
    cur = _item_of(items, "fastmetal", "fastmetal-1.3b-qad")
    assert "不是能力轨当前选中的那一档" not in cur["status_detail"]


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


def test_远程自建也能留验证痕(as_admin, clean_registrations):
    """0045：远程自建（backend='remote'）的记录能落库，且**只认它自己名下的**。

    0045 之前这条是反过来的——`model_validation` 的 CHECK 只认 local/cloud，
    于是这一档的"环境检查"直接 422，目录里它的 kinds 被硬置成空集，
    状态永远停在 unverified，每次都要向人解释"不是坏了"。

    这一条要钉三件事：
    ① 环境检查能跑完并留痕（结论是 unknown——本机不查别人的文件系统），
       且记录写的是 `backend='remote'` 不是折回后的 local；
    ② `unknown` **不算证据**，所以它进不了 `validation_kinds`（这是对的，
       不是漏了）——真正能证明 remote 可用的是 real_call／artifact_review；
    ③ 本地同 provider 同模型档的记录不许替它背书。
    """
    rid = as_admin.post("/api/models", json={
        "capability": "image", "backend": "remote", "provider": "mflux",
        "model_code": "flux1-schnell"}).json()["id"]

    # ① 环境检查能跑完、能留痕，且记录在 remote 名下
    r = as_admin.post(f"/api/models/{rid}/check")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend"] == "remote", \
        f"记录要写在登记行自己的轴上；写成 local 会混进本地验证史：{body}"
    assert body["kind"] == "env_check"
    assert body["state"] == "unknown", \
        "本机查不了别人的机器，环境检查的结论只能是 unknown"

    # ② unknown 不算证据：这一档仍然不是「可用」
    item = _item_of_remote(as_admin.get("/api/models").json()["items"],
                           "mflux", "flux1-schnell")
    assert item["validation_kinds"] == [], \
        f"unknown 不该被当成可用凭据：{item['validation_kinds']}"
    assert item["status"] == "unverified"

    # ③ 本地同 provider 同模型档的记录不许替它背书
    val_repo.record({"capability": "image", "backend": "local", "provider": "mflux",
                     "model": "flux1-schnell", "kind": "real_call", "state": "ok"})
    item = _item_of_remote(as_admin.get("/api/models").json()["items"],
                           "mflux", "flux1-schnell")
    assert "real_call" not in item["validation_kinds"], \
        "本地记录不能当成远程自建的凭据——两条轴各记各的"
    assert item["status"] == "unverified"

    # ④ 它自己的 real_call 才是凭据：remote 的健康恒 unknown，但那不再挡路
    val_repo.record({"capability": "image", "backend": "remote", "provider": "mflux",
                     "model": "flux1-schnell", "kind": "real_call", "state": "ok"})
    item = _item_of_remote(as_admin.get("/api/models").json()["items"],
                           "mflux", "flux1-schnell")
    assert "real_call" in item["validation_kinds"]
    assert item["status"] == "available", \
        f"有真调用凭据就该给可用，否则这一档永远等一个不可能到来的健康结论：" \
        f"{item['status_detail']}"


def _item_of_remote(items, provider, model_code):
    """按 provider／model 找那一行——remote 与本地可能同 provider，所以还要看 backend。"""
    for it in items:
        if (it["provider"] == provider and it["model_code"] == model_code
                and it["backend"] == "remote"):
            return it
    raise AssertionError(f"目录里没有 remote 那一行：{provider}/{model_code}")


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


def test_必需能力必须落在标签全集里(as_admin):
    """必需能力放开成可编辑后，造一个清单里没有的标签会把场景推到
    "永远 0 候选"还不报错——写入侧按 producible 全集拦一次。"""
    r = as_admin.put("/api/model-policies", json=[{
        "scene_key": "scene_prompt", "capability": "text",
        "required": ["文本生成", "不存在的标签"]}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "不存在的标签" in detail and "标签全集" in detail, detail
    # 清单里真有的标签照常放行：把必需项降下来是允许的（§5.4 的原话）
    ok = as_admin.put("/api/model-policies", json=[{
        "scene_key": "scene_prompt", "capability": "text",
        "required": ["文本生成"]}])
    assert ok.status_code == 200, ok.text
    # 降成空清单也允许：这个场景从此不按必需能力筛候选
    ok = as_admin.put("/api/model-policies", json=[{
        "scene_key": "scene_prompt", "capability": "text", "required": []}])
    assert ok.status_code == 200, ok.text


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
    # 声明了能力但清单里没有的那条：点名"清单里没有这一档"，而不是假装它不存在。
    # 措辞必须让人看出"改配置没用、要去补规格"——这正是三个「未登记」改名要解决的事。
    assert any("flux1-dev" in b and "清单里没有这一档" in b for b in out["blockers"]), \
        out["blockers"]


def test_目录读路径不出网(as_admin, clean_registrations, monkeypatch):
    """打开一页不许替人连网：`GET /api/models` 一次都不许调 engines.probe。

    这条边界是 2026-11-07 审查点名的第三根管子：原先前端一页要拼两遍目录，
    每行再各探一次服务型引擎（超时 5 秒、串行、无缓存），一台卡住的服务就能把
    整个列表接口拖到超时。现在探测只有一个显式入口——`POST /api/models/probe`。
    """
    import engines as engines_mod

    calls = []
    real = engines_mod.probe

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(engines_mod, "probe", spy)
    import cloud_probe

    cloud_calls = []

    def cloud_spy(*args, **kwargs):
        cloud_calls.append(args)
        return []

    monkeypatch.setattr(cloud_probe, "probe_registered", cloud_spy)
    engines_mod.forget_probes()
    from repositories.engine_instances import engine_instances

    instance = engine_instances.create({"engine_code": "draw_things",
                                        "endpoint": "http://127.0.0.1:7871"})
    try:
        as_admin.post("/api/models", json={
            "capability": "image", "backend": "local", "provider": "draw_things",
            "model_code": "noobai-xl-epsilon-11"})
        body = as_admin.get("/api/models").json()
        assert calls == [], f"目录读路径不该发网络请求，实际调了 {calls}"
        assert cloud_calls == [], f"目录读路径也不该碰云端探测，实际调了 {cloud_calls}"
        item = _item_of(body["items"], "draw_things", "noobai-xl-epsilon-11")
        # 有实例但没查过：这一档要说"要连一次才知道"，并且带上"平台会查"的标记，
        # 而不是伪装成 unknown（那是"平台查不了"，两件事的下一步不一样）。
        assert item["health"]["probe_deferred"] is True
        assert "要连一次服务才知道" in item["health"]["detail"]
    finally:
        engine_instances.delete(instance["id"])


def test_探测接口现查之后目录里那一格跟着变(as_admin, clean_registrations, monkeypatch):
    """`POST /api/models/probe` 是唯一会连网的那条路：它把状态写回整张目录。"""
    import engines as engines_mod

    monkeypatch.setattr(engines_mod, "probe", lambda code, endpoint="", **_: engines_mod.EngineStatus(
        "ok", "服务在线，当前模型 x.ckpt",
        {"endpoint": endpoint or "http://127.0.0.1:7860", "model": "x.ckpt"}))
    engines_mod.forget_probes()
    as_admin.post("/api/models", json={
        "capability": "image", "backend": "local", "provider": "draw_things",
        "model_code": "noobai-xl-epsilon-11"})
    probed = as_admin.post("/api/models/probe?fresh=1").json()
    item = _item_of(probed["items"], "draw_things", "noobai-xl-epsilon-11")
    assert item["health"]["state"] == "ok"
    assert probed["stats"]["registered_total"] == len(
        [r for r in probed["items"] if r["registered"]]), "四数必须与同一份 items 对得上"
    assert [p["engine_code"] for p in probed["probed"]] == ["draw_things"]
    # 现查之后同一次读的目录里那一格就是 ok，不再是"待现查"
    assert as_admin.get("/api/models").json()["items"] is not None


def test_探测接口把云端段一起带回(as_admin, clean_registrations, monkeypatch):
    """`cloud` 段是 2026-11-08 补的口径：按钮点了，云端也得有动静。

    这里不打真供应商：`probe_registered` 换成假造——测的是**接线**（路由把
    登记行交给云端探测、结果原样进响应），真探测的五档在 test_cloud_probe.py。
    """
    import cloud_probe

    seen = {}

    def fake_registered(rows, *, fresh=False):
        seen["rows"] = rows
        seen["fresh"] = fresh
        return [{"provider": "deepseek", "label": "DeepSeek（官方 API）",
                 "capability": "text", "key_env": "DEEPSEEK_API_KEY",
                 "url": "https://api.deepseek.com/models", "has_key": True,
                 "state": "ok", "cached": False,
                 "detail": "端点应答、密钥被接受（HTTP 200）"}]

    monkeypatch.setattr(cloud_probe, "probe_registered", fake_registered)
    as_admin.post("/api/models", json={
        "capability": "text", "backend": "cloud", "provider": "deepseek",
        "model_code": "deepseek-flash"})
    probed = as_admin.post("/api/models/probe?fresh=1").json()
    assert seen["fresh"] is True, "按钮那一枪是现查，云端也得绕过缓存"
    assert [r["backend"] for r in seen["rows"] if r["backend"] == "cloud"] == ["cloud"]
    assert [c["provider"] for c in probed["cloud"]] == ["deepseek"]
    assert probed["cloud"][0]["state"] == "ok"


def test_结构化输出这条标签有清单来源(as_admin, clean_registrations):
    """`结构化输出` 原来没有任何来源，而 0035 把它写进 4 个场景的必需能力。

    现在它来自 `ModelSpec.structured_output`：只有运行时真的会按 schema 约束输出的
    档才带（本机 Ollama 会下发 format）。这条用例同时钉住反方向——云端 DeepSeek
    今天没有 response_format 分支，就不许带这个标签。
    """
    as_admin.post("/api/models", json={"capability": "text", "backend": "local",
                                       "provider": "ollama", "model_code": "qwen3:8b"})
    as_admin.post("/api/models", json={"capability": "text", "backend": "cloud",
                                       "provider": "deepseek",
                                       "model_code": "deepseek-flash"})
    items = as_admin.get("/api/models").json()["items"]
    ollama = {t["tag"] for t in _item_of(items, "ollama", "qwen3:8b")["tags"]}
    deepseek = {t["tag"] for t in _item_of(items, "deepseek", "deepseek-flash")["tags"]}
    assert "结构化输出" in ollama and "文本生成" in ollama
    assert "结构化输出" not in deepseek, "云端没有 response_format 分支，不许带这个标签"


def test_清单里根本产不出来的必需能力必须单独说(as_admin, clean_registrations):
    """「配不出来」与「还没配」要说不同的话。

    character_variant 要「图生图 + 参考图」，而 image 轴没有任何一档登记过图片
    输入参数——这在任何配置下都不可能凑齐。把它写成"没有候选"会让人反复去配，
    而真正要动的是 `adapters.py`。
    """
    out = as_admin.get("/api/model-policies/resolve/character_variant").json()
    assert out["required_producible"] is False
    assert set(out["impossible_tags"]) == {"图生图", "参考图"}
    joined = " ".join(out["blockers"])
    assert "没有任何来源" in joined and "adapters.py" in joined, out["blockers"]


def test_场景要求的标签都能产出时就不报不可达(as_admin, clean_registrations):
    """反方向半边：scene_prompt 只要「文本生成」，清单里有，就不许挂那条结论。"""
    out = as_admin.get("/api/model-policies/resolve/scene_prompt").json()
    assert out["required_producible"] is True and out["impossible_tags"] == []


def test_候选接口明写不自动切换(as_admin):
    out = as_admin.get("/api/model-policies/resolve/scene_prompt").json()
    assert out["auto_switch"] is False
    assert "不会自动顶上" in out["policy_note"]


def test_人工声明不能替代清单事实(as_admin, clean_registrations):
    """手工声明「图生图 + 参考图」不能把 4 个数学上无解的场景救活。

    审查里实测过这一条：给 mflux 手工声明这两个标签后 character_variant 立刻出候选——
    那等于"用一句自我声明冒充能力证明"，与这一页"健康≠验证"的纪律自相矛盾。
    判据是清单：image 轴这一版没有任何一档登记过图片输入参数，所以这两个标签
    在 image 轴上不存在任何合法来源；声明了也进不了候选，而且要说清是为什么。
    """
    as_admin.post("/api/models", json={
        "capability": "image", "backend": "local", "provider": "mflux",
        "model_code": "flux1-schnell", "capabilities": ["图生图", "参考图"]})
    as_admin.put("/api/model-policies", json=[{
        "scene_key": "character_variant", "capability": "image",
        "required": ["图生图", "参考图"]}])
    out = as_admin.get("/api/model-policies/resolve/character_variant").json()
    assert out["candidates"] == [], "清单里没有来源的能力，声明了也不许进候选"
    blocker = next(b for b in out["blockers"] if "flux1-schnell" in b)
    assert "人工声明" in blocker and "清单" in blocker, blocker


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
    """ComfyUI 本机装了但平台零引用：必须出现在页面上，
    但不能混进"引擎总数"，更不能有一个能点的连接按钮。

    Draw Things 已从这张名单里出去（2026-11-06 W-B 接入），所以这里同时钉住
    反方向的那半件事：它现在必须在清单侧的 `items` 里，而不是在未接入名单里。
    只测"comfyui 还在"是不够的——那等于允许有人把已接入的引擎一直挂着"未接入"。
    """
    body = as_admin.get("/api/models").json()
    s = as_admin.get("/api/models/stats").json()
    codes = {e["code"] for e in body["unintegrated_engines"]}
    assert "comfyui" in codes
    assert "draw-things" not in codes, "Draw Things 已接入，别再挂在未接入名单里"
    assert codes.isdisjoint({i["engine_code"] for i in body["items"]})
    assert codes.isdisjoint({i["provider"] for i in body["items"]})
    assert any(i["provider"] == "draw_things" for i in body["items"])
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


def test_验证历史能翻页且说得出总量(as_admin):
    """第 201 条之后的历史原来读不到，而且没有任何提示。

    钉三件事：① 总量走响应头（界面不必拿"少于 limit"猜有没有下一页）；
    ② `before_id` 能把更早的翻出来，且与第一页不重叠；
    ③ 翻到底返回空数组而不是报错——"没有更早的了"是一种正常结果。
    """
    # 造出"多于一页"的历史：一页上限是仓储层的 MAX_PAGE，直接用它的值 + 5。
    from repositories.validation import validations as vrepo

    slot = {"capability": "text", "backend": "cloud", "provider": "paging-test"}
    before = vrepo.count()
    for i in range(vrepo.MAX_PAGE + 5):
        vrepo.record({**slot, "kind": "env_check", "state": "missing",
                            "detail": f"翻页用例第 {i} 条"})
    total = vrepo.count()
    assert total == before + vrepo.MAX_PAGE + 5

    first = as_admin.get("/api/providers/validations?limit=10")
    assert first.status_code == 200
    assert int(first.headers["X-Total-Count"]) == total
    page1 = first.json()
    assert len(page1) == 10

    # 第二页：以第一页最后一条的 id 为界，避开头尾相接
    second = as_admin.get(
        f"/api/providers/validations?limit=10&before_id={page1[-1]['id']}")
    page2 = second.json()
    assert len(page2) == 10
    assert not ({v["id"] for v in page1} & {v["id"] for v in page2}), "两页重叠了"
    assert max(v["id"] for v in page2) < min(v["id"] for v in page1)

    # 翻过最早那一条之后是空的，不是 4xx
    oldest = as_admin.get("/api/providers/validations?limit=1&before_id=1").json()
    assert oldest == []


def test_一页上限由仓储说而不是路由手抄(as_admin):
    """`limit=100000` 不许真的回十万条：分页边界只有仓储一处定义。

    原来这条夹逼写在仓储的 `min(int(limit), 200)` 里，而路由只管把 limit 转发；
    这一版把它提成 `MAX_PAGE` 常量并让翻页也用它，所以这里断言的是
    "请求一个荒谬的 limit，回来的条数不超过仓储说的那一页"。
    """
    from repositories.validation import validations as vrepo

    r = as_admin.get(f"/api/providers/validations?limit={vrepo.MAX_PAGE + 500}")
    assert r.status_code == 200
    assert len(r.json()) <= vrepo.MAX_PAGE


def test_撤登记与撤实例都不再要求中文口令(as_admin):
    """`confirm=撤登记`／`confirm=撤实例` 是把界面文案写进了协议。

    改成只认"人表达过确认"这件事（1／true／yes，大小写不敏感）。
    要钉的是**边界没松**：不带 confirm 仍然 422，乱填仍然 422。
    """
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad"}).json()["id"]
    assert as_admin.delete(f"/api/models/{rid}").status_code == 422
    assert as_admin.delete(f"/api/models/{rid}?confirm=no").status_code == 422
    # 中文旧口令也不再接受：它从来不是协议的一部分，只是当年顺手写下的字符串
    assert as_admin.delete(f"/api/models/{rid}?confirm=撤登记").status_code == 422
    # 大写与 "yes" 都算确认过
    assert as_admin.delete(f"/api/models/{rid}?confirm=YES").status_code == 200

    made = as_admin.post("/api/engines", json={
        "engine_code": "draw_things", "endpoint": "http://127.0.0.1:7861"}).json()
    eid = made["id"]
    assert as_admin.delete(f"/api/engines/{eid}").status_code == 422
    assert as_admin.delete(f"/api/engines/{eid}?confirm=0").status_code == 422
    assert as_admin.delete(f"/api/engines/{eid}?confirm=true").status_code == 200


def test_选路三件事留变更痕_撤登记在案(as_admin, clean_registrations):
    """0044：审计不做（v0.87 裁定），但 is_default 是选路的唯一来源，
    它变了没人说得清是谁、之前是什么——三个写点各记一行，先钉撤登记这笔。

    不外键是有意的：登记行没了，痕还得 standalone 地站着。"""
    rid = as_admin.post("/api/models", json={
        "capability": "video", "backend": "local", "provider": "fastmetal",
        "model_code": "fastmetal-1.3b-qad"}).json()["id"]
    assert as_admin.delete(f"/api/models/{rid}?confirm=1").status_code == 200

    out = as_admin.get("/api/config-changes").json()
    hit = [r for r in out["items"]
           if r["subject"] == "model_registration"
           and r["subject_key"] == "video/local/fastmetal/fastmetal-1.3b-qad"]
    assert hit, f"撤登记要留痕：{out['items'][:3]}"
    assert hit[0]["action"] == "delete"
    assert str(rid) in str(hit[0]["payload"])


def test_能力轨保存只记变了的行且点名默认轨变更(as_admin, provider_state):
    """整批保存是十行一起 PUT：痕只记实际变了的行，否则"发生了什么"
    被九行没变的噪声淹掉。默认轨的 before/after 要进 payload。"""
    rows = as_admin.get("/api/providers").json()
    # seed 里 video/local 本来就是默认（0014 归位）但 provider/model 是空串——
    # 把它配上：provider 与 model 都变，这才是"给默认轨填上真身"这一步。
    body = []
    for r in rows:
        item = {k: r[k] for k in ("id", "provider", "model", "enabled", "is_default")}
        item["key_env"] = r["key_env_custom"]
        item["notes"] = r["notes"]
        if r["capability"] == "video" and r["backend"] == "local":
            item.update(provider="fastmetal", model="fastmetal-1.3b-qad", enabled=True)
        body.append(item)
    assert as_admin.put("/api/providers", json=body).status_code == 200

    out = as_admin.get("/api/config-changes").json()
    hits = [r for r in out["items"] if r["subject"] == "provider_slot"]
    video_hits = [r for r in hits if r["subject_key"] == "video/local"]
    assert video_hits, f"video/local 的默认轨变更要留痕：{hits[:3]}"
    summary = video_hits[0]["summary"]
    assert "fastmetal" in summary and "fastmetal-1.3b-qad" in summary, summary
    # 没变的行不记：痕里不该出现 text/local（这一批它什么都没改）
    assert not [r for r in hits if r["subject_key"] == "text/local"], \
        "没变的行不该进痕——那是在制造噪声"


def test_清单轴上只折一次(as_admin):
    """`remote` 折回 `local` 的判据只许有一处，而且要下发给界面。

    原来三份实现：路由建登记时一句、`model_center._manifest_axis()` 一句、
    前端 `manifestProviders()` 第三句。三份一起漂的症状是上一轮那个
    「remote 登记在列表里显示清单未登记」。这一版收进 `adapters.MANIFEST_AXIS`
    并由 `manifest_payload()` 下发，界面按数据折。
    """
    import adapters

    payload = as_admin.get("/api/providers/manifest").json()
    assert payload["axis"] == adapters.MANIFEST_AXIS
    assert payload["axis"]["remote"] == "local"
    # 折回判据不许在前端再写一遍字面量
    # 2026-11-08 拆分后 manifestAxis 住在 models/shared.js
    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "views"
          / "models" / "shared.js").read_text()
    assert "=== 'remote' ? 'local'" not in js, "界面还在自己折"
    assert "manifestAxis(" in js
    # 后端也只留一处：路由里不许再出现那个三元表达式
    routers_src = (Path(__file__).resolve().parents[1] / "routers"
                   / "model_center.py").read_text()
    assert '"local" if backend == "remote" else backend' not in routers_src


def test_三个未登记的状态名说得出缺哪一侧(as_admin):
    """「未登记」「清单未登记」「只登记了名字」三个词都含"登记"，处置却相反。

    钉两件事：① 状态键与中文名都按"缺的是哪一侧"取（`manifest_only`／
    `registration_only`／`name_only`）；② 三种行在同一张目录里真的分得开——
    不是换了名字但算出来的还是同一档。
    """

    labels = as_admin.get("/api/models").json()["status_labels"]
    # 旧的三个键一个都不许留：留着就说明还有一条路径在产出旧值
    for gone in ("not_registered", "unregistered_tier"):
        assert gone not in labels, f"旧状态键 {gone} 还在"
    assert labels["manifest_only"] == "本机未登记"
    assert labels["registration_only"] == "清单无此档"
    assert labels["name_only"] == "清单只有名字"
    # 三个名字互不为前缀，也不共用"未登记"这个词
    names = [labels["manifest_only"], labels["registration_only"], labels["name_only"]]
    assert len(set(names)) == 3
    assert not any("未登记" in n for n in names[1:]), names

    items = as_admin.get("/api/models").json()["items"]
    seen = {i["status"] for i in items}
    # 目录里天然带着"清单有、本机没登记"的档（图片×本地的那些模型档）
    assert "manifest_only" in seen
    # 与登记行无关的两种在这里不必然出现，所以用清单/登记各造一条
    as_admin.post("/api/models", json={
        "capability": "image", "backend": "local",
        "provider": "zzz-not-in-manifest", "model_code": "v1"})
    after = as_admin.get("/api/models").json()["items"]
    orphan = next((i for i in after
                   if i["provider"] == "zzz-not-in-manifest"), None)
    assert orphan is not None and orphan["status"] == "registration_only"
    # 只登记了名字的壳：清单里有 adapter 但没有模型档
    shell = next((i for i in after if i["status"] == "name_only"), None)
    assert shell is not None, "清单里那些只有名字的 adapter 应当各占一行"


def test_清单无此档的挡路原因说的是去补规格(as_admin):
    """不能只说"没登记"——那一档点多少次「登记」都不会变好。

    这是三词改名真正的产出：候选弹窗里那句挡路原因必须让人看出
    "改配置没用、要去 adapters.py"。
    """
    as_admin.post("/api/models", json={
        "capability": "text", "backend": "cloud",
        "provider": "zzz-nowhere", "model_code": "x"})
    out = as_admin.get("/api/model-policies/resolve/storyboard").json()
    # 挡路原因里那一档的名字取的是显示名／模型标签／模型码（可能是 "x"，
    # 不带 provider），所以按措辞找而不是按 provider 找。
    hit = [b for b in out["blockers"] if "清单里没有这一档" in b]
    assert hit, out["blockers"]
    assert "adapters.py" in hit[0], "得说清去哪补，而不是只说缺"
    # 反证：老的措辞（只说"没登记"）不该再出现——那句话让人去点登记
    assert not any("清单未登记这一档" in b for b in out["blockers"])


def test_连接方式只让选真的有实现的那几档(as_admin):
    """`grpc`／`cli` 是"存得进、选得到、但没有任何行为"的假选项（CR-010）。

    0040 的 CHECK 还收它们（缩了会让库里已有行读不出来），所以两把分开：
    `CONNECTION_TYPES` 是存储域、`SUPPORTED_CONNECTION_TYPES` 是能选的那一把。
    写入口只收后者，且拒绝理由要说清"得先写探测与执行分支"。
    """
    from repositories.engine_instances import CONNECTION_TYPES, SUPPORTED_CONNECTION_TYPES

    # CHECK 那一把不许缩：缩了库里已有的行就成孤儿
    assert set(CONNECTION_TYPES) >= {"local_http", "grpc", "cli"}
    assert SUPPORTED_CONNECTION_TYPES == ("local_http",)

    listing = as_admin.get("/api/engines").json()
    # 下发给界面的只有能选的那一把；全集另给一个键，好让"库里有历史行"这件事说得清
    assert listing["connection_types"] == list(SUPPORTED_CONNECTION_TYPES)
    assert set(listing["declared_connection_types"]) == set(CONNECTION_TYPES)

    r = as_admin.post("/api/engines", json={
        "engine_code": "draw_things", "endpoint": "http://127.0.0.1:7870",
        "connection_type": "grpc"})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "还没有实现分支" in detail and "local_http" in detail
    # 拼错的东西走另一句话：两种拒绝混成一句，人只会以为是自己填错
    r2 = as_admin.post("/api/engines", json={
        "engine_code": "draw_things", "endpoint": "http://127.0.0.1:7871",
        "connection_type": "websocket"})
    assert r2.status_code == 422
    assert "还没有实现分支" not in r2.json()["detail"]


def test_非_http_连接方式的实例不假装探过(as_admin):
    """库里若有历史行是 `grpc`，读路径不许拿一次 HTTP 的失败当它的结论。

    `engines.probe()` 只看 engine_code，不读 connection_type——它一律按 HTTP 打。
    所以列表必须自己按连接方式分流，并明说"这一档还没有探测分支"。
    """
    import db
    import engines

    # 绕过写入口造一条历史行（写入口已经拒了；这里模拟"当年存进去的"）
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO engine_instance
                   (engine_code, display_name, connection_type, endpoint, enabled)
               VALUES (?, ?, ?, ?, 1)""",
            ("draw_things", "历史 grpc 行", "grpc", "http://127.0.0.1:7872"))
        iid = cur.lastrowid
    engines.forget_probes()
    try:
        # 把探测打成"连不上"：如果读路径真的去探了，这一行就会显示连不上
        engines.probe = lambda *a, **k: engines.EngineStatus("missing", "假的连不上", {})
        hit = next(i for i in as_admin.get("/api/engines").json()["items"]
                   if i["id"] == iid)
        assert hit["probe_unsupported"] is True
        assert hit["health_state"] == "unknown"
        assert "还没有探测分支" in hit["health_detail"]
        assert "假的连不上" not in hit["health_detail"], "拿一次 HTTP 的结论冒充了"
    finally:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM engine_instance WHERE id = ?", (iid,))


def payload_with(held_rows, notes):
    """按「我手上那一版」拼提交体：令牌与内容都取自 held_rows。"""
    body = [{"id": r["id"], "provider": r["provider"], "model": r["model"],
             "enabled": r["enabled"], "is_default": r["is_default"],
             "key_env": r["key_env_custom"], "notes": r["notes"],
             "seen_version": r["version"]} for r in held_rows]
    body[0]["notes"] = notes
    return body


def test_整批覆盖配置有弱版本闸(as_admin, provider_state):
    """`PUT /api/providers` 是十行整批覆盖：两人各改一行先后保存会静默互相盖。

    闸只在这一条上开：带了 `seen_version` 才判，对不上回 409 并点名是哪几行。
    不带令牌的调用（脚本、命令行）行为一字不变——这是与资产页同一条边界。
    """
    rows = as_admin.get("/api/providers").json()
    assert all("version" in r and r["version"] for r in rows), "每行都要带令牌"

    def payload(notes, with_token=True):
        """按**当前** rows 拼一份提交体。notes 覆盖第一行，用来区分"谁写的"。"""
        body = [{"id": r["id"], "provider": r["provider"], "model": r["model"],
                 "enabled": r["enabled"], "is_default": r["is_default"],
                 "key_env": r["key_env_custom"], "notes": r["notes"]}
                for r in rows]
        body[0]["notes"] = notes
        if with_token:
            for item, r in zip(body, rows):
                item["seen_version"] = r["version"]
        return body

    # ① 不带令牌：照旧 200（脚本与命令行调用方的契约不变）
    assert as_admin.put("/api/providers", json=payload("甲写的", False)).status_code == 200
    rows = as_admin.get("/api/providers").json()      # 重新取最新令牌

    # ② 带**新**令牌：通过
    assert as_admin.put("/api/providers", json=payload("甲写的")).status_code == 200
    mine = as_admin.get("/api/providers").json()      # 我此刻持有的那一版

    # ③ 别人在我之后改了一行（换一份令牌写进去）
    assert as_admin.put("/api/providers",
                        json=payload_with(mine, "乙改的")).status_code == 200

    # ④ 我拿**旧**令牌提交我的改动：必须 409，且点名哪一行被改过
    stale = payload_with(mine, "甲想盖回去")
    r = as_admin.put("/api/providers", json=stale)
    assert r.status_code == 409, r.text
    assert "被别人改过" in r.json()["detail"]
    # ⑤ 冲突时整批一个字节都没写：库里是乙那句，不是我这句
    assert as_admin.get("/api/providers").json()[0]["notes"] == "乙改的"


# ---------- 场景骨架：生成中心的提交框要能自己列场景（2026-11-07） ----------
def test_场景骨架登录即可读且不含建议轨(as_role, seeded):
    """普通编辑者站在提交框前面，得能列出九个场景——但只该看到"要什么能力"。

    为什么这条值得单独钉：`GET /api/model-policies` 是 system:config，
    于是生成中心这一格原来只能由管理员口头告诉人填哪个 scene_key。
    把场景骨架降到登录即可的同时，**建议轨与备用轨必须一起挡住**：
    那两个字段一旦下发，前端很容易顺手画成"已选中"，而裁定 MC1 是只提示不自动切。
    """
    c = as_role("editor")            # 有 job:submit，没有 system:config
    r = c.get("/api/model-policies/scenes")
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"]
    assert len(items) == 9, f"场景数定死在 0035 种子里，现在是 {len(items)}"
    keys = {it["scene_key"] for it in items}
    assert "shot_video" in keys and "storyboard" in keys and "character_variant" in keys
    shot = next(it for it in items if it["scene_key"] == "shot_video")
    assert shot["capability"] == "video"
    assert shot["capability_label"] == "视频"
    assert shot["required"] == ["文生视频"]
    # 配置项一个都不许漏出来：这是"骨架"，不是"策略"
    for forbidden in ("slot_provider", "slot_model", "fallback_provider",
                      "fallback_model", "preference", "cloud_confirm_required"):
        assert all(forbidden not in it for it in items), \
            f"场景骨架把配置项 {forbidden} 发出去了：那会让人以为平台替你选了轨"


def test_场景骨架把配不出来与还没配分开(as_admin, clean_registrations):
    """`character_variant` 要「图生图 + 参考图」，image 轴这一版产不出来。

    两个数在界面上说不同的话：`required_producible=false` 是要改 adapters.py，
    true 只是还没登记、还没点探测。混成一句"待配置"，人就会对着一个
    永远点不亮的场景反复配——2026-11-07 审查里四个场景正是这样。
    """
    items = as_admin.get("/api/model-policies/scenes").json()["items"]
    variant = next(it for it in items if it["scene_key"] == "character_variant")
    assert variant["required_producible"] is False
    assert set(variant["impossible_required"]) == {"图生图", "参考图"}
    # 对照组：分镜生成要的两项清单里都产得出来，它只是"还没配"
    story = next(it for it in items if it["scene_key"] == "storyboard")
    assert story["required_producible"] is True
    assert story["impossible_required"] == []
