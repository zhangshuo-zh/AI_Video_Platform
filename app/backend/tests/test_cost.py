"""W08：成本账的四条硬口径。

  * 云端任务必须先确认量 × 价，金额由服务端算；
  * 失败不默认把成本清零；
  * 多币种分开汇总，绝不跨币种相加；
  * 重试新建一行、原行成本不动；本地作业记机时。

这里不跑模型：成本是 Job 的属性，用仓储层直接造"已失败/已成功"的行，
比让 worker 假装调云端更接近要验的东西。
"""
import worker
from repositories.jobs import jobs


def _put_video(c, local=None, cloud=None):
    rows = c.get("/api/providers").json()
    body = []
    for r in rows:
        item = {k: r[k] for k in ("id", "provider", "model", "enabled", "is_default")}
        item["key_env"] = r["key_env_custom"]
        item["notes"] = r["notes"]
        if r["capability"] == "video" and r["backend"] == "local" and local:
            item.update(local)
        if r["capability"] == "video" and r["backend"] == "cloud" and cloud:
            item.update(cloud)
        body.append(item)
    resp = c.put("/api/providers", json=body)
    assert resp.status_code == 200, resp.text


def _cloud_video(c, project_id, **kw):
    body = {
        "project_id": project_id, "capability": "video", "prompt": "云端一镜",
        "provider_code": "wan", "model_code": "wan-1080p",
        "execution_mode": "cloud", "params": {},
        "user_confirmed_cloud": True,
        "estimate": {"quantity": 2.5, "unit": "second",
                     "unit_price_micros": 1_200_001,
                     "currency": "CNY", "source": "供应商官网 2026-11-01"},
    }
    body.update(kw)
    return c.post("/api/jobs", json=body)


def _make_job(project_id, **over):
    data = {
        "project_id": project_id, "capability": "video", "kind": "video_cloud",
        "execution_mode": "cloud", "provider_code": "wan", "model_code": "wan-1080p",
        "adapter_version": "test", "prompt_snapshot": "成本测试",
        "params": {}, "input_refs": [], "created_by": None,
        "quantity": 4.0, "unit": "second", "unit_price_micros": 1_000_000,
        "cost_micros": 4_000_000, "currency": "CNY",
        "pricing_snapshot": {"source": "测试"}, "billable_status": "pending_reconciliation",
    }
    data.update(over)
    return jobs.create(data)


def test_云端必须先确认量价_金额由服务端算(as_role, provider_state, clean_queue, fresh_project):
    pid = fresh_project()
    admin = as_role("admin")
    _put_video(admin, cloud={"provider": "wan", "model": "wan-1080p", "enabled": True})
    c = as_role("editor")

    # 只勾确认、不给量价：不落库
    no_price = _cloud_video(c, pid, estimate=None)
    assert no_price.status_code == 422
    assert "预计成本" in no_price.json()["detail"]

    # 只给量价、不勾确认：同样不落库（顺序上先问确认）
    unconfirmed = _cloud_video(c, pid, user_confirmed_cloud=False)
    assert unconfirmed.status_code == 422
    assert "确认" in unconfirmed.json()["detail"]

    # 2.5 × 1,200,001 = 3,000,002.5 → half-up 到 3,000,003 micros
    ok = _cloud_video(c, pid)
    assert ok.status_code == 202, ok.text
    data = ok.json()
    assert data["cost_micros"] == 3_000_003
    assert data["currency"] == "CNY"
    assert data["billable_status"] == "pending_reconciliation"

    detail = c.get(f"/api/jobs/{data['id']}").json()
    assert detail["pricing_snapshot"]["unit_price_micros"] == 1_200_001
    assert detail["pricing_snapshot"]["source"].startswith("供应商官网")
    assert detail["pricing_snapshot"]["quantity"] == "2.5"


def test_本地作业不收预计成本(as_role, clean_queue, fresh_project):
    pid = fresh_project()
    c = as_role("editor")
    r = c.post("/api/jobs", json={
        "project_id": pid, "capability": "video", "prompt": "本地",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.0},
        "estimate": {"quantity": 1, "unit": "request", "unit_price_micros": 1,
                     "currency": "CNY", "source": "不该出现"},
    })
    assert r.status_code == 422
    assert "本地作业不计费" in r.json()["detail"]


def test_失败不把成本清零_重试原行不动(as_role, clean_queue, fresh_project):
    pid = fresh_project()
    job = _make_job(pid)
    claimed = jobs.claim_next("cost-worker", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    jobs.fail(job["id"], "cost-worker", "provider_error", "供应商 500", elapsed_ms=900)

    row = jobs.get(job["id"])
    assert row["status"] == "failed"
    assert row["cost_micros"] == 4_000_000          # 失败不归零
    assert row["billable_status"] == "pending_reconciliation"

    new = jobs.retry(row, None)
    assert new["id"] != row["id"]
    assert new["retry_of_job_id"] == row["id"]
    assert new["cost_micros"] == 4_000_000          # 重试继承同一份计价快照
    assert new["billable_status"] == "pending_reconciliation"
    assert jobs.get(job["id"])["cost_micros"] == 4_000_000   # 原行仍不动


def test_多币种分开汇总_不跨币种相加(as_role, clean_queue, fresh_project):
    pid = fresh_project()
    _make_job(pid, cost_micros=4_000_000, currency="CNY")
    _make_job(pid, cost_micros=2_500_000, currency="CNY")
    _make_job(pid, cost_micros=1_500_000, currency="USD")
    # 排队中取消的云端作业：量价还在详情里，但不进金额
    cancelled = _make_job(pid)
    assert jobs.request_cancel(cancelled["id"])["billable_status"] == "not_billable"

    c = as_role("editor")
    data = c.get(f"/api/jobs/costs?project_id={pid}").json()
    buckets = {b["currency"]: b for b in data["currencies"]}
    assert set(buckets) == {"CNY", "USD"}
    assert buckets["CNY"]["total_micros"] == 6_500_000
    assert buckets["CNY"]["jobs"] == 2
    assert buckets["USD"]["total_micros"] == 1_500_000
    assert buckets["USD"]["jobs"] == 1
    assert data["total_jobs"] == 4


def test_排队取消不计费但不抹掉当初的预计(as_role, clean_queue, fresh_project):
    pid = fresh_project()
    job = _make_job(pid)
    updated = jobs.request_cancel(job["id"])
    assert updated["status"] == "cancelled"
    assert updated["billable_status"] == "not_billable"
    assert updated["cost_micros"] == 4_000_000      # 详情里仍看得见预计
    c = as_role("editor")
    data = c.get(f"/api/jobs/costs?project_id={pid}").json()
    assert data["currencies"] == []                  # 但账上为 0


def test_本地作业记录耗时与资源(as_role, clean_queue, fresh_project):
    pid = fresh_project()
    c = as_role("editor")
    r = c.post("/api/jobs", json={
        "project_id": pid, "capability": "video", "prompt": "本地机时",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.05},
    })
    assert r.status_code == 202, r.text
    assert worker.run_once("cost-worker") is not None

    detail = c.get(f"/api/jobs/{r.json()['id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["elapsed_ms"] is not None and detail["elapsed_ms"] >= 0
    res = detail["resources"]
    assert res["wall_ms"] >= 0
    assert res["cpu_ms"] >= 0
    assert res["max_rss_kb"] > 0
    # macOS 的 ru_maxrss 是字节、Linux 是 KB；归一后一个 worker 不可能报到
    # 千万 KB（10GB）以上。这条挡住"把字节当 KB 存、界面显示 21344 MB"的回归。
    assert res["max_rss_kb"] < 10_000_000
    assert detail["billable_status"] == "not_billable"

    data = c.get(f"/api/jobs/costs?project_id={pid}").json()
    assert data["local"]["jobs"] == 1
    assert data["local"]["elapsed_ms"] >= 0
    assert data["local"]["max_rss_kb"] > 0
    assert data["currencies"] == []


def test_项目中心只带只读累计(as_role, clean_queue, fresh_project):
    pid = fresh_project()
    _make_job(pid, cost_micros=4_000_000, currency="CNY")
    c = as_role("editor")
    me = c.get("/api/projects").json()
    row = next(p for p in me if p["id"] == pid)
    assert row["costs"][0]["currency"] == "CNY"
    assert row["costs"][0]["total_micros"] == 4_000_000
    assert row["costs"][0]["pending"] == 1
