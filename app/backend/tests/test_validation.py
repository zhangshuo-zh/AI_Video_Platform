"""P0：模型验证的 provenance 分离（外部评审点名的那条口径事故）。

要守住的是一句话：**"配置就绪"不等于"真的能产出"**。所以这里逐条钉死：

- 环境检查（env_check）通过，永远不能把 real_call 那一格点亮；
- mock 跑绿一次，不能记成真实调用——否则测试环境的绿会冒充生产可用；
- 真实调用必须有一条成功作业 + 一份读得到的产物做凭据；
- 产物验收（artifact_review）记的是人看过文件后的结论，通过与否必须明说，
  不通过也算一条记录（state=failed），但不能因此把生产可用点亮；
- "生产可用"只由服务端按 环境齐 ∧ 真调用过 ∧ 产物验收通过 算出来，界面不自己拼。

这些用例都不打真供应商、不加载权重：真实调用凭据用一条本地成功作业 + 落盘产物，
环境检查的结论复用 adapters.health()，是什么就是什么，不替它美化。
"""
import hashlib

import pytest

import jobfiles
import validations
from repositories.jobs import jobs as job_repo
from repositories.providers import providers as provider_repo
from repositories.validation import validations as val_repo


def _succeeded_job(pid, *, capability="video", execution_mode="local",
                   provider="fastmetal", model="fastmetal-1.3b-qad",
                   artifact: bytes | None = b"fake-video-bytes",
                   confirmed_cloud=False, request_id=None):
    """造一条已经 succeeded 的作业；artifact=None 表示产物文件不在盘上。"""
    # kind 受 CHECK 约束：video 还要分本地/云端，其余与能力同名
    kind = {"video": f"video_{execution_mode}"}.get(capability, capability)
    row = job_repo.create({
        "project_id": pid, "capability": capability, "kind": kind,
        "execution_mode": execution_mode, "provider_code": provider,
        "model_code": model, "adapter_version": "test",
        "prompt_snapshot": "验证用", "params": {}, "input_refs": [],
        "user_confirmed_cloud": confirmed_cloud,
    })
    claimed = job_repo.claim_next("val-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    paths = []
    if artifact is not None:
        _logs, outputs = jobfiles.ensure_dirs(row["id"])
        target = outputs / "out.bin"
        target.write_bytes(artifact)
        paths = [jobfiles.relative(target)]
    job_repo.succeed(row["id"], "val-worker", "", paths, 1,
                     provider_request_id=request_id)
    return job_repo.get(row["id"])


def test_环境检查不点亮真实调用那一格(fresh_project):
    """env_check 跑完，real_call 仍是 never，生产可用仍是 False。"""
    slot = provider_repo.slot("text", "cloud")
    assert slot is not None
    result = validations.env_check(slot, created_by=None)
    assert result["validation"]["kind"] == "env_check"
    summary = validations.summary_for(slot)
    assert summary["real_call"]["state"] == "never"
    assert summary["production_ready"] is False


def test_mock_成功不能记成真实调用(fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _succeeded_job(pid, execution_mode="local", provider="mock", model="mock-1")
    with pytest.raises(validations.ValidationInvalid) as exc:
        validations.from_job(job["id"], "real_call", created_by=None)
    assert "假适配器" in str(exc.value)
    # 产物验收同一条规矩：假适配器跑出的文件再好看，也不是真模型产出
    with pytest.raises(validations.ValidationInvalid) as exc:
        validations.from_job(job["id"], "artifact_review", created_by=None, passed=True)
    assert "假适配器" in str(exc.value)


def test_真实作业产物不可读时拒记(fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _succeeded_job(pid, artifact=None)
    with pytest.raises(validations.ValidationInvalid) as exc:
        validations.from_job(job["id"], "real_call", created_by=None)
    assert "产物文件已不可读" in str(exc.value)


def test_真实本地成功登记_real_call_带产物指纹(fresh_project, clean_queue):
    pid = fresh_project("editor")
    payload = b"real-local-artifact-123456"
    job = _succeeded_job(pid, artifact=payload)
    result = validations.from_job(job["id"], "real_call", created_by=None)
    rec = result["validation"]
    assert rec["kind"] == "real_call" and rec["state"] == "ok"
    assert rec["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
    import json
    checks = json.loads(rec["checks_json"])
    assert checks["artifact_bytes"] == len(payload)
    assert checks["artifact_sha256_present"] is True
    assert checks["execution_mode"] == "local"


def test_云端真实调用要先有计费确认(fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _succeeded_job(pid, capability="text", execution_mode="cloud",
                         provider="deepseek", model="deepseek-flash",
                         artifact=b"cloud-text", confirmed_cloud=False,
                         request_id="chatcmpl-x")
    with pytest.raises(validations.ValidationInvalid) as exc:
        validations.from_job(job["id"], "real_call", created_by=None)
    assert "计费确认" in str(exc.value)
    # 补上确认后才放行
    from db import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE job SET user_confirmed_cloud = 1 WHERE id = ?", (job["id"],))
    ok = validations.from_job(job["id"], "real_call", created_by=None)
    assert ok["validation"]["paid_confirmed"] == 1


def test_生产可用要环境齐且真调用过且产物验收通过():
    """直接喂同槽位记录，验服务端口径的阶梯：三档齐了才算，缺一档都不行。"""
    slot = {"capability": "image", "backend": "local", "provider": "mflux"}
    val_repo.record({**slot, "model": "m", "kind": "env_check", "state": "ok"})
    summary = validations.summary_for(slot)
    assert summary["production_ready"] is False          # 只有环境检查
    assert validations.badge(summary)[0] == "env_check"

    val_repo.record({**slot, "model": "m", "kind": "real_call", "state": "ok"})
    summary = validations.summary_for(slot)
    # 环境齐＋真调用过还不够：链路产出过文件不等于文件被人看过
    assert summary["production_ready"] is False
    assert validations.badge(summary)[0] == "real_call"

    val_repo.record({**slot, "model": "m", "kind": "artifact_review", "state": "ok"})
    summary = validations.summary_for(slot)
    assert summary["production_ready"] is True
    # 徽章按可信度从高到低：产物验收比真实调用多一道人眼
    assert validations.badge(summary)[0] == "artifact_review"

    val_repo.record({**slot, "model": "m", "kind": "target_machine", "state": "ok"})
    summary = validations.summary_for(slot)
    assert summary["target_machine_ready"] is True
    # 目标机基准仍是最高档
    assert validations.badge(summary)[0] == "target_machine"


def test_产物验收必须有明确结论(fresh_project, clean_queue):
    """passed 没给就拒记：这一档记的是人的判断，服务端不猜、不默认。"""
    pid = fresh_project("editor")
    job = _succeeded_job(pid, artifact=b"to-be-reviewed")
    with pytest.raises(validations.ValidationInvalid) as exc:
        validations.from_job(job["id"], "artifact_review", created_by=None)
    assert "必须给出结论" in str(exc.value)


def test_产物验收不通过也留痕且不点亮生产可用(fresh_project, clean_queue,
                                              provider_state):
    pid = fresh_project("editor")
    slot = provider_repo.slot("video", "local")
    # 先把槽位指到要验的那一档（0043：summary 按"当前那一档"取记录——
    # 槽位没填 model 时按档取会落进"model=空串"那一组，与按 1.3B 落的记录对不上；
    # 这也正是换档不背书想要的形状）。
    provider_repo.replace_many([{
        "id": slot["id"], "provider": "fastmetal", "model": "fastmetal-1.3b-qad",
        "enabled": True, "is_default": True, "key_env": "", "notes": ""}])
    slot = provider_repo.slot("video", "local")
    prov = slot["provider"]
    job = _succeeded_job(pid, artifact=b"broken-frames", provider=prov)
    # 先把三档铺垫齐，再记一次"不通过"，看生产可用是否被错误点亮
    val_repo.record({"capability": "video", "backend": "local",
                     "provider": prov, "model": "fastmetal-1.3b-qad",
                     "kind": "env_check", "state": "ok"})
    val_repo.record({"capability": "video", "backend": "local",
                     "provider": prov, "model": "fastmetal-1.3b-qad",
                     "kind": "real_call", "state": "ok"})
    rec = validations.from_job(job["id"], "artifact_review", created_by=None,
                               passed=False, detail="花屏")["validation"]
    assert rec["kind"] == "artifact_review" and rec["state"] == "failed"
    assert rec["job_id"] == job["id"]
    assert "花屏" in rec["detail"]
    summary = validations.summary_for(slot)
    assert summary["artifact_review"]["state"] == "failed"
    assert summary["production_ready"] is False
    # 不通过也算一次验收：徽章仍回落到真实调用那一档
    assert validations.badge(summary)[0] == "real_call"
    # 人再验一次通过了，最新记录说话，生产可用才点亮
    job2 = _succeeded_job(pid, artifact=b"fixed-frames", provider=prov)
    ok = validations.from_job(job2["id"], "artifact_review", created_by=None,
                              passed=True)["validation"]
    assert ok["state"] == "ok"
    summary = validations.summary_for(slot)
    assert summary["production_ready"] is True


def test_换档后槽位的生产可用不背别的档():
    """0043 的正主用例：同一 provider 验过 1.3B，槽位换到 5B 之后，
    能力轨那一格不许再亮——「生产可用」认的是**当前档自己的记录**，
    而不是这一 provider 历史上随便哪一档。这与模型仓库的 same_model
    判据是同一把尺子（换档不背书的全链兑现）。"""
    slot = {"capability": "video", "backend": "local", "provider": "fastmetal"}
    for kind in ("env_check", "real_call", "artifact_review"):
        val_repo.record({**slot, "model": "fastmetal-1.3b-qad", "kind": kind, "state": "ok"})
    # 槽位还指着 1.3B：三档都亮
    now = {**slot, "model": "fastmetal-1.3b-qad"}
    assert validations.summary_for(now)["production_ready"] is True

    # 槽位换到 5B：一条记录都没变，但生产可用必须熄灭
    after = {**slot, "model": "fastmetal-5b-qad"}
    summary = validations.summary_for(after)
    assert summary["production_ready"] is False
    assert summary["real_call"]["state"] == "never", \
        "5B 自己没有记录，就该是 never——不许拿 1.3B 的记录冒充"
    assert validations.badge(summary)[0] == "never"


def test_产物验收要产物可读且云端要先确认计费(fresh_project, clean_queue):
    pid = fresh_project("editor")
    # 产物不在盘上：链路产出过的证据都没了，人眼验的是哪个文件？
    job = _succeeded_job(pid, artifact=None)
    with pytest.raises(validations.ValidationInvalid) as exc:
        validations.from_job(job["id"], "artifact_review", created_by=None, passed=True)
    assert "产物文件已不可读" in str(exc.value)
    # 云端作业没确认计费：与真实调用同一条规矩
    cloud = _succeeded_job(pid, capability="text", execution_mode="cloud",
                           provider="deepseek", model="deepseek-flash",
                           artifact=b"cloud-text", confirmed_cloud=False)
    with pytest.raises(validations.ValidationInvalid) as exc:
        validations.from_job(cloud["id"], "artifact_review", created_by=None, passed=True)
    assert "计费确认" in str(exc.value)


def test_mock_徽章永远排在真实调用之下():
    slot = {"capability": "tts", "backend": "local", "provider": "piper"}
    val_repo.record({**slot, "model": "m", "kind": "mock", "state": "ok"})
    val_repo.record({**slot, "model": "m", "kind": "real_call", "state": "ok"})
    summary = validations.summary_for(slot)
    assert validations.badge(summary)[0] == "real_call"
    # mock 通过也不等于生产可用（还缺环境检查和产物验收）
    assert summary["production_ready"] is False


def test_验证记录只增不改(fresh_project, clean_queue):
    slot = {"capability": "music", "backend": "local", "provider": "ace"}
    first = val_repo.record({**slot, "model": "m", "kind": "env_check", "state": "ok"})
    second = val_repo.record({**slot, "model": "m", "kind": "env_check", "state": "missing"})
    assert second["id"] > first["id"]
    latest = val_repo.latest_by_kind("music", "local", "ace")["env_check"]
    assert latest["id"] == second["id"] and latest["state"] == "missing"
    # 旧记录还在历史里，没被改写
    assert val_repo.latest("music", "local", "ace", "env_check")["id"] == second["id"]


# ---------- 接口层：两个写入入口各自只收自己那一类 ----------

def test_validate_入口只收_env_check(as_role):
    c = as_role("admin")
    r = c.post("/api/providers/validate",
               json={"capability": "text", "backend": "cloud", "kind": "real_call"})
    assert r.status_code == 422
    assert "只做环境检查" in r.json()["detail"]


def test_validate_job_入口不收_env_check(as_role, seeded):
    c = as_role("admin")
    r = c.post(f"/api/providers/validate-job/{seeded['job_id']}",
               json={"capability": "video", "backend": "local", "kind": "env_check"})
    assert r.status_code == 422
    assert "环境检查不走作业" in r.json()["detail"]


def test_validate_job_入口产物验收必须有结论(as_role, seeded):
    """接口层也不许漏传 passed：body 里没有就是没有，服务端不补默认值。"""
    c = as_role("admin")
    r = c.post(f"/api/providers/validate-job/{seeded['job_id']}",
               json={"capability": "video", "backend": "local", "kind": "artifact_review"})
    assert r.status_code == 422
    assert "必须给出结论" in r.json()["detail"]


def test_环境检查接口留痕且不带密钥值(as_role, provider_state, monkeypatch):
    c = as_role("admin")
    slot = provider_repo.slot("text", "cloud")
    # 往这一轨的密钥变量里塞一个假值：响应里只该有"变量在不在"，绝不能有这个值
    planted = "sk-plant-secret-do-not-leak"
    if slot["key_env"]:
        monkeypatch.setenv(slot["key_env"], planted)
    r = c.post("/api/providers/validate",
               json={"capability": "text", "backend": "cloud",
                     "provider": slot["provider"], "kind": "env_check"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "env_check"
    assert planted not in r.text
    history = c.get("/api/providers/validations?limit=5").json()
    assert any(v["id"] == body["id"] for v in history)
    assert all(planted not in str(v) for v in history)