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


# ---------- 第 3 条：人工标记不能绕过状态机 ----------

def test_mark_attention_走状态机_运行中的作业不会被钉进终态(
        fresh_project, clean_queue, people):
    """`running` 的作业不能被人工标记改写成 needs_attention。

    这条钉的是 2026-11-09 外部审查第 3 条。原来的实现是
    「事务外读状态 → 裸 UPDATE `WHERE id=?`」：
      - 服务层读到的若是 failed，就在事务外放行了；
      - 等 UPDATE 落下时，别人（worker 领取/重投）可能已把它推成 running，
        那条不带状态条件的 UPDATE 照样把正在跑的作业钉进 needs_attention。
    现在仓储层在 `transaction()` 里读-断言-写，且显式只收终态。

    注意两件事：

    ① `job_state.TRANSITIONS` 里 `running → needs_attention` 是**合法**的
       （worker 收尾要用），所以状态机本身不拦人工标记走这一路；
    ② 服务层的 `mark_job_attention` 事务外先看了一眼状态，无竞争时它就会
       把 running/queued 挡回去——**但那一眼读的是会过期的快照**，
       真正的竞争窗口在"它读完"与"仓储层写下去"之间。

    所以这条测试绕过服务层，直接打仓储。这正是第 3 条修的位置：
    原来仓储层是裸 UPDATE（不带状态条件、不看 rowcount），
    现在在 `transaction()` 里读-断言-写，且显式只收终态。
    """
    pid = fresh_project("editor")
    job = _cloud_job(pid, status_via="succeed")
    # 摆回 running：模拟"别处把它推起来了"
    with get_conn() as conn:
        conn.execute("UPDATE job SET status='running' WHERE id=?", (job["id"],))
    with pytest.raises(Exception):          # 仓储层的 JobStateConflict
        job_repo.mark_attention_by_user(job["id"], "看到产物不对")
    assert job_repo.get(job["id"])["status"] == "running", "不许被改成 needs_attention"


def test_mark_attention_排队中的作业也不能被标记(fresh_project, clean_queue, people):
    """`queued` 连 `needs_attention` 都转不过去，标记必须明确失败。"""
    pid = fresh_project("editor")
    uid = {"id": people["editor"]["id"]}
    job = job_repo.create({
        "project_id": pid, "capability": "text", "kind": "text",
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "adapter_version": "t",
        "prompt_snapshot": "排队中", "params": {}, "input_refs": [],
        "cost_micros": 1000, "currency": "CNY",
        "billable_status": "pending_reconciliation",
    })
    assert job["status"] == "queued"
    # 同样直接打仓储：服务层那一眼会先挡，但仓储层自己也必须挡
    with pytest.raises(Exception):
        job_repo.mark_attention_by_user(job["id"], "想提前挂起")
    assert job_repo.get(job["id"])["status"] == "queued"


# ---------- 第 5 条：后到的核对必须明说"没写进去" ----------

def test_reconcile_后到的人收到冲突而不是成功(fresh_project, clean_queue, people):
    """两个人先后核对同一条时，第二个人不能以为自己的决定生效了。

    2026-11-08 就给那条 UPDATE 加了 `AND status='needs_attention'`，
    但命中 0 行时什么都没说——路由照样 200，服务层拼的 message 还是
    按「我要做什么」写的（"已确认可用…"）。于是后到者看到一句成功提示，
    库里留的却是前一个人的结论。第 5 条把这个窟窿补上：明确抛冲突。
    """
    pid = fresh_project("editor")
    a = {"id": people["editor"]["id"]}
    b = {"id": people["owner"]["id"]} if "owner" in people else a
    job = _cloud_job(pid, status_via="attention")

    services.reconcile_job(job["id"], JobReconcileIn(
        decision="succeeded", actual_cost_micros=1234, note="甲认为可用"), a)
    first = job_repo.get(job["id"])
    assert first["status"] == "succeeded" and first["actual_cost_micros"] == 1234

    with pytest.raises(services.JobStateConflict):
        services.reconcile_job(job["id"], JobReconcileIn(
            decision="failed", actual_cost_micros=9999, note="乙认为不可用"), b)

    still = job_repo.get(job["id"])
    assert still["status"] == "succeeded", "甲的决定必须原样保留"
    assert still["actual_cost_micros"] == 1234, "金额不能被后到者覆盖"

    # **关键的一半：绕过服务层直接打仓储。**
    # 上面那次 409 其实被服务层事务外的 `status != needs_attention` 挡下了，
    # 走不到仓储层的判定；而真正的竞争窗口在"服务层读完"到"仓储层写下去"
    # 之间——那时服务层看到的是旧快照，只有仓储层自己再判一次才拦得住。
    # 回退第 5 条的实现（早退分支 return row 而不是抛冲突）时，
    # 只有这一段会变红。
    with pytest.raises(Exception):
        job_repo.reconcile(job["id"], "failed", 9999, "乙绕服务层", b["id"])
    last = job_repo.get(job["id"])
    assert last["status"] == "succeeded"
    assert last["actual_cost_micros"] == 1234


# ---------- 第 10 条：running_jobs 的可见性一次判完，判据不变 ----------

def test_worker_status_只报自己看得见的正在跑的作业(
        fresh_project, clean_queue, people):
    """顶栏小圆点里"正在跑哪条"的裁剪，一次查完，判据与 ensure 一致。

    原来是 `for row in running_jobs: ensure(user, row["project_id"], "read")`，
    每行至少回库一次（实测 16 条 running 时 8.43 ms 全花在这串上）。
    改成一次读出"自己拥有的 ∪ 作为成员加入的"之后，这条测试钉的是
    **判据没变**：别人的项目看不见，自己的看得见，管理员全看得见。
    """
    # "别人的项目"用 admin 作 owner、以 editor 的视角看：判据是
    # "自己拥有的 ∪ 作为成员加入的"，editor 既不是 owner 也不是成员，看不见。
    #
    # 刻意**不用** `fresh_project("outsider")`：`people["outsider"]` 是共享身份，
    # 给它建项目会让 test_jobs 里"外人看到空列表"那条断言变成顺序相关的假红。
    mine = fresh_project("editor")
    other = fresh_project("admin")
    job_repo.create({
        "project_id": mine, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal", "model_code": "m",
        "adapter_version": "t", "prompt_snapshot": "我的", "params": {}, "input_refs": [],
    })
    job_repo.create({
        "project_id": other, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "fastmetal", "model_code": "m",
        "adapter_version": "t", "prompt_snapshot": "别人的", "params": {}, "input_refs": [],
    })
    # 把两条都摆成 running，才进得了 running_jobs
    with get_conn() as conn:
        conn.execute("""UPDATE job SET status='running',
                                   lease_until=datetime('now','localtime','+1 hour')
                         WHERE status='queued'""")

    # `user_rank` 要的是 app_user 整行（看 is_superuser），而 `people[]`
    # 里只有 id/token/name，所以这里回库取一次完整行。
    def _row(uid):
        with get_conn() as conn:
            return conn.execute("SELECT * FROM app_user WHERE id = ?", (uid,)).fetchone()

    editor = services.worker_status(_row(people["editor"]["id"]))
    seen = {row["project_id"] for row in editor["running_jobs"]}
    assert mine in seen, "自己的项目必须看得见"
    assert other not in seen, "别人的项目不许顺着顶栏漏出去"

    # 超级用户全放行（`visible_project_ids` 返回 None 那一支）
    superadmin = services.worker_status(_row(people["superadmin"]["id"]))
    both = {row["project_id"] for row in superadmin["running_jobs"]}
    assert {mine, other} <= both, "超级用户看得见全部"

    # **把摆成 running 的两条收回去**：`clean_queue` 只在测试开始前清一次，
    # 不管收尾。留着 running 会让后面的用例（如 test_jobs 的权限断言）
    # 在自己的项目列表里看到这两条，变成顺序相关的假红。
    with get_conn() as conn:
        conn.execute("""UPDATE job SET status='cancelled',
                                   finished_at=datetime('now','localtime'),
                                   error_code='test_cleanup'
                         WHERE status='running' AND project_id IN (?, ?)""",
                     (mine, other))
