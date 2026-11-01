"""P0：模型验证的 provenance 分离（外部评审点名的那条口径事故）。

要守住的是一句话：**"配置就绪"不等于"真的能产出"**。所以这里逐条钉死：

- 环境检查（env_check）通过，永远不能把 real_call 那一格点亮；
- mock 跑绿一次，不能记成真实调用——否则测试环境的绿会冒充生产可用；
- 真实调用必须有一条成功作业 + 一份读得到的产物做凭据；
- "生产可用"只由服务端按 环境齐 ∧ 真调用过 算出来，界面不自己拼。

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


def test_生产可用要环境齐且真调用过():
    """直接喂两条同槽位记录，验服务端口径，不依赖本机健康结论。"""
    slot = {"capability": "image", "backend": "local", "provider": "mflux"}
    val_repo.record({**slot, "model": "m", "kind": "env_check", "state": "ok"})
    summary = validations.summary_for(slot)
    assert summary["production_ready"] is False          # 只有环境检查
    assert validations.badge(summary)[0] == "env_check"

    val_repo.record({**slot, "model": "m", "kind": "real_call", "state": "ok"})
    summary = validations.summary_for(slot)
    assert summary["production_ready"] is True
    assert validations.badge(summary)[0] == "real_call"

    val_repo.record({**slot, "model": "m", "kind": "target_machine", "state": "ok"})
    summary = validations.summary_for(slot)
    assert summary["target_machine_ready"] is True
    # 徽章按可信度从高到低：目标机 > 真实调用
    assert validations.badge(summary)[0] == "target_machine"


def test_mock_徽章永远排在真实调用之下():
    slot = {"capability": "tts", "backend": "local", "provider": "piper"}
    val_repo.record({**slot, "model": "m", "kind": "mock", "state": "ok"})
    val_repo.record({**slot, "model": "m", "kind": "real_call", "state": "ok"})
    summary = validations.summary_for(slot)
    assert validations.badge(summary)[0] == "real_call"
    # mock 通过也不等于生产可用（还缺环境检查）
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