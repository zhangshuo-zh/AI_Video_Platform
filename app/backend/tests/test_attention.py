"""P0：worker 异常告警与云端"待核对"人工闭环。

外部评审的第二条要害：**云端结果未知时，绝不能把"重试"当成免费默认动作**。
供应商可能已经建了任务、甚至已经计了费。所以这条链路是：

    结果可疑 → needs_attention（成本列不动）→ 人核对 → 账单结清

这里逐条钉死：
- recover_stale 把"有供应商请求 ID 的云端作业"挂成待核对，而不是 failed；
- reconcile 只对待核对生效、只对云端生效、确认结果必须给实际金额；
- mark_attention 让人把"回执说成功、产物却打不开"的作业主动挂起来；
- retry-preview 在重试前把"这一串已经计了多少、会不会再花一次"摆给人看；
- worker_status 的告警只报事实，不改任何 Job 状态。

全部用仓储层造数据，不打真供应商、不真跑模型。
"""
import json
from datetime import datetime, timedelta

import pytest

import services
from db import get_conn
from models import JobReconcileIn
from repositories.jobs import jobs as job_repo


def _cloud_job(pid, *, status_via="fail", cost=16_000, request_id="req-1",
               confirmed=True, priced=True):
    """造一条云端作业并收成一个终态。status_via: fail / succeed / attention。"""
    row = job_repo.create({
        "project_id": pid, "capability": "text", "kind": "text",
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "adapter_version": "test",
        "prompt_snapshot": "待核对样本", "params": {}, "input_refs": [],
        "quantity": 0.002 if priced else None,
        "unit": "million_token" if priced else None,
        "unit_price_micros": 8_000_000 if priced else None,
        "cost_micros": cost if priced else None,
        "currency": "CNY" if priced else None,
        "billable_status": "pending_reconciliation" if priced else "not_billable",
        "pricing_snapshot": {"source": "测试"} if priced else None,
        "user_confirmed_cloud": confirmed,
    })
    claimed = job_repo.claim_next("att-worker", 60)
    assert claimed is not None and claimed["id"] == row["id"]
    if status_via == "fail":
        job_repo.fail(row["id"], "att-worker", "provider_auth", "模拟失败",
                      provider_request_id=request_id)
    elif status_via == "succeed":
        job_repo.succeed(row["id"], "att-worker", "", [], 1,
                         provider_request_id=request_id)
    elif status_via == "attention":
        job_repo.needs_attention(row["id"], "att-worker", "provider_timeout",
                                 "结果未知", "云端调用超时", provider_request_id=request_id)
    return job_repo.get(row["id"])


# ---------- reconcile：三条硬规矩 ----------

def test_reconcile_只对待核对生效(fresh_project, clean_queue, people):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="succeed")
    with pytest.raises(services.JobStateConflict) as exc:
        services.reconcile_job(job["id"], JobReconcileIn(decision="pending"),
                               {"id": people["editor"]["id"]})
    assert "不是待核对" in str(exc.value)


def test_reconcile_本地作业拒核(fresh_project, clean_queue, people):
    pid = fresh_project("editor")
    row = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal",
        "model_code": "m", "adapter_version": "t", "prompt_snapshot": "x",
        "params": {}, "input_refs": [],
    })
    job_repo.claim_next("att-worker", 60)
    job_repo.needs_attention(row["id"], "att-worker", "worker_lost", "x", "本地")
    with pytest.raises(services.JobStateConflict) as exc:
        services.reconcile_job(row["id"], JobReconcileIn(decision="pending"),
                               {"id": people["editor"]["id"]})
    assert "只有云端作业" in str(exc.value)


def test_reconcile_确认结果必须给金额(fresh_project, clean_queue, people):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="attention")
    with pytest.raises(services.JobStateConflict) as exc:
        services.reconcile_job(job["id"], JobReconcileIn(decision="succeeded"),
                               {"id": people["editor"]["id"]})
    assert "必须填实际金额" in str(exc.value)


def test_reconcile_succeeded_账单转known(fresh_project, clean_queue, people):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="attention")
    assert job["billable_status"] == "pending_reconciliation"
    uid = people["editor"]["id"]
    services.reconcile_job(job["id"],
                           JobReconcileIn(decision="succeeded",
                                          actual_cost_micros=12_345, note="后台核过"),
                           {"id": uid})
    done = job_repo.get(job["id"])
    assert done["status"] == "succeeded"
    assert done["billable_status"] == "known"
    assert done["actual_cost_micros"] == 12_345
    assert done["reconciled_by"] == uid
    assert done["reconcile_note"] == "后台核过"
    # 预计金额（成本快照）不被核对改写
    assert done["cost_micros"] == 16_000


def test_reconcile_failed_也按实际金额结清(fresh_project, clean_queue, people):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="attention")
    services.reconcile_job(job["id"],
                           JobReconcileIn(decision="failed", actual_cost_micros=0),
                           {"id": people["editor"]["id"]})
    done = job_repo.get(job["id"])
    assert done["status"] == "failed"
    assert done["billable_status"] == "known"
    assert done["actual_cost_micros"] == 0


def test_reconcile_pending_只动备注(fresh_project, clean_queue, people):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="attention")
    services.reconcile_job(job["id"],
                           JobReconcileIn(decision="pending", note="再等等供应商"),
                           {"id": people["editor"]["id"]})
    done = job_repo.get(job["id"])
    assert done["status"] == "needs_attention"
    assert done["billable_status"] == "pending_reconciliation"
    assert done["reconcile_note"] == "再等等供应商"


# ---------- mark_attention：把可疑的成功主动挂起来 ----------

def test_mark_attention_只收云端已结束(fresh_project, clean_queue, people):
    pid = fresh_project("editor")
    uid = {"id": people["editor"]["id"]}
    # 本地成功 → 拒
    local = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal", "model_code": "m",
        "adapter_version": "t", "prompt_snapshot": "x", "params": {}, "input_refs": [],
    })
    job_repo.claim_next("att-worker", 60)
    job_repo.succeed(local["id"], "att-worker", "", [], 1)
    with pytest.raises(services.JobStateConflict):
        services.mark_job_attention(local["id"], "产物打不开", uid)
    # 云端成功 → 收成待核对
    cloud = _cloud_job(pid, status_via="succeed")
    services.mark_job_attention(cloud["id"], "回执说成功但产物打不开", uid)
    done = job_repo.get(cloud["id"])
    assert done["status"] == "needs_attention"
    assert "产物打不开" in done["attention_reason"]


# ---------- recover_stale：有请求 ID 的云端不能直接算 failed ----------

def test_recover_stale_云端有请求ID挂待核对(fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="attention", request_id="req-keep")
    # 重新摆回 running + 过期租约，模拟 worker 中途消失
    with get_conn() as conn:
        conn.execute(
            """UPDATE job SET status='running', provider_request_id='req-keep',
                      lease_until=datetime('now','localtime','-1 hour'),
                      worker_id='ghost' WHERE id=?""", (job["id"],))
    result = job_repo.recover_stale()
    assert result["needs_attention"] >= 1
    done = job_repo.get(job["id"])
    assert done["status"] == "needs_attention"
    assert done["error_code"] == "worker_lost"


def test_recover_stale_云端无请求ID算failed(fresh_project, clean_queue):
    pid = fresh_project("editor")
    row = job_repo.create({
        "project_id": pid, "capability": "text", "kind": "text",
        "execution_mode": "cloud", "provider_code": "deepseek", "model_code": "m",
        "adapter_version": "t", "prompt_snapshot": "x", "params": {}, "input_refs": [],
    })
    job_repo.claim_next("att-worker", 60)
    with get_conn() as conn:
        conn.execute(
            """UPDATE job SET status='running', provider_request_id=NULL,
                      lease_until=datetime('now','localtime','-1 hour'),
                      worker_id='ghost' WHERE id=?""", (row["id"],))
    job_repo.recover_stale()
    assert job_repo.get(row["id"])["status"] == "failed"


# ---------- retry-preview：重试前把账摆给人看 ----------

def test_retry_preview_云端提示可能再计费(fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="fail")
    preview = services.retry_preview(job["id"])
    assert preview["may_charge_again"] is True
    assert preview["execution_mode"] == "cloud"
    assert "待核对" in preview["warning"]
    assert preview["chain_total_micros"] == 16_000
    assert any(r["id"] == job["id"] for r in preview["chain"])


def test_retry_preview_本地不计费(fresh_project, clean_queue):
    pid = fresh_project("editor")
    row = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal", "model_code": "m",
        "adapter_version": "t", "prompt_snapshot": "x", "params": {}, "input_refs": [],
    })
    job_repo.claim_next("att-worker", 60)
    job_repo.fail(row["id"], "att-worker", "adapter_failed", "x")
    preview = services.retry_preview(row["id"])
    assert preview["may_charge_again"] is False
    assert "本地重试不计费" in preview["warning"]


def test_retry_写attempt与superseded链(fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="fail")
    again = job_repo.retry(job_repo.get(job["id"]), created_by=None)
    assert again["attempt"] == 2
    assert again["retry_of_job_id"] == job["id"]
    assert again["billable_status"] == "pending_reconciliation"
    original = job_repo.get(job["id"])
    assert original["superseded_by_job_id"] == again["id"]
    assert original["retry_count"] == 1
    chain = job_repo.retry_chain(again["id"])
    assert [r["id"] for r in chain] == [job["id"], again["id"]]
    job_repo.request_cancel(again["id"])


# ---------- 接口层：状态冲突回 409，预览回继承与计价 ----------

def test_reconcile_接口_非待核对回409(as_role, fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="succeed")
    c = as_role("editor")
    r = c.post(f"/api/jobs/{job['id']}/reconcile", json={"decision": "pending"})
    assert r.status_code == 409


def test_attention_接口_云端成功转待核对(as_role, fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="succeed")
    c = as_role("editor")
    r = c.post(f"/api/jobs/{job['id']}/attention", json={"reason": "产物打不开"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "needs_attention"


def test_retry_preview_接口_回继承与计价(as_role, fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="fail")
    c = as_role("editor")
    r = c.get(f"/api/jobs/{job['id']}/retry-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["may_charge_again"] is True
    assert body["provider_code"] == "deepseek"
    assert body["chain"]


# ---------- worker 告警：只报事实，不改状态 ----------

def test_worker_告警_待核对计数(fresh_project, clean_queue):
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="attention")
    status = services.worker_status()
    assert status["needs_attention"] >= 1
    assert any(a["code"] == "needs_attention" for a in status["alerts"])
    # 告警不改状态：那条作业仍是待核对，没被偷偷重试或失败
    assert job_repo.get(job["id"])["status"] == "needs_attention"


def test_worker_告警_离线还有排队(monkeypatch, fresh_project, clean_queue):
    pid = fresh_project("editor")
    job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal", "model_code": "m",
        "adapter_version": "t", "prompt_snapshot": "排队", "params": {}, "input_refs": [],
    })
    # 没有心跳＝离线
    monkeypatch.setattr(services.job_repo, "worker_state", lambda: None)
    status = services.worker_status()
    assert status["online"] is False
    assert any(a["code"] == "worker_offline" and a["level"] == "bad"
               for a in status["alerts"])


def test_worker_告警_运行超时与日志静默(fresh_project, clean_queue):
    pid = fresh_project("editor")
    row = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal", "model_code": "m",
        "adapter_version": "t", "prompt_snapshot": "卡住", "params": {}, "input_refs": [],
    })
    job_repo.claim_next("att-worker", 60)
    old = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE job SET status='running', started_at=?, updated_at=? WHERE id=?",
                     (old, old, row["id"]))
    status = services.worker_status()
    codes = {a["code"] for a in status["alerts"]}
    assert "run_timeout" in codes
    assert "log_silent" in codes
    # 超时只告警，不替人把作业判失败
    assert job_repo.get(row["id"])["status"] == "running"