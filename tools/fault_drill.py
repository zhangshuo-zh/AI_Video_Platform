"""Job 故障演练脚本（评审 §3.3 的七条，零成本、可重复）。

为什么做成脚本而不是只写在文档里：演练要能重放。换一台机器、换一个版本，
跑一遍就知道这些语义有没有被改坏；结论只从真实代码路径里来，不靠人背。

三条边界：

1. **全部打临时库**：FASTVIDEO_DB/FILES/JOBS/WORKSPACE 都指到系统临时目录，
   import config 之前设好（config 在导入时就把路径读成常量，晚设等于没设）。
   真实的 data/platform.db、稿子工作区、产物目录一概不碰。
2. **不打真供应商、不花一分钱**：本地作业用假适配器（只 sleep、写一个文本结果），
   云端语义用仓储层造数；产物校验用手工文件喂给 runners.verify()。
3. **结论只报观测**：每条演练打印"做了什么 → 看到什么"，断言失败即非零退出。

用法：app/backend/.venv/bin/python tools/fault_drill.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "app" / "backend"
sys.path.insert(0, str(BACKEND))

# ↓ 环境必须先于任何项目 import：config 在导入时读死路径
_TMP = Path(tempfile.mkdtemp(prefix="fv-drill-"))
os.environ["FASTVIDEO_DB"] = str(_TMP / "drill.db")
os.environ["FASTVIDEO_FILES"] = str(_TMP / "files")
os.environ["FASTVIDEO_WORKSPACE"] = str(_TMP / "workspace")
os.environ["FASTVIDEO_JOBS"] = str(_TMP / "jobs")
# 假适配器是演练里唯一"真的会跑"的本地路线：只 sleep、写文本，不碰模型权重
os.environ["FASTVIDEO_ALLOW_MOCK"] = "1"
# 只活在这条一次性演练库里的口令，与任何真实部署无关
os.environ["FASTVIDEO_ADMIN_PASSWORD"] = "drill-only-not-a-real-secret"

from datetime import datetime, timedelta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import adapters  # noqa: E402
import services  # noqa: E402
from db import get_conn  # noqa: E402
from main import app  # noqa: E402
from models import JobSubmitIn  # noqa: E402
from repositories.jobs import jobs as job_repo  # noqa: E402
from repositories.projects import projects as project_repo  # noqa: E402
from repositories.users import sessions, users as user_repo  # noqa: E402
from security import hash_password, new_session_token  # noqa: E402
import runners  # noqa: E402

PASSED: list[str] = []


def ok(title: str, detail: str) -> None:
    """记一条演练结论。只报观测到的事实，不做任何修饰。"""
    print(f"  → {title}\n    {detail}")
    PASSED.append(title)


def db_exec(sql: str, *args) -> None:
    """演练里的"时间机器"：改租约、改心跳都用它。只打临时库，不碰真实数据。"""
    with get_conn() as conn:
        conn.execute(sql, args)


def backdate_lease(job_id: int, seconds: int = 600) -> None:
    """把租约拨回到过去——等效于"worker 死了很久，没人续租"。"""
    past = (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    db_exec("UPDATE job SET lease_until = ? WHERE id = ?", past, job_id)


def main() -> None:
    client = TestClient(app)
    # 铸一个超大会话（同测试地基做法：不真登录，直接塞 cookie）
    from bootstrap import ensure_admin
    ensure_admin(verbose=False)
    admin = user_repo.get_by_username("superadmin")
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, admin["id"], expires)
    client.cookies.set("fv_session", token)
    pid = project_repo.create("故障演练", "fault_drill 用，可整库丢弃",
                              [], admin["id"])["id"]

    # ---------- 演练 1：本地任务运行中 worker 被强制终止 ----------
    print("[演练 1] 本地任务运行中 worker 被强制终止，重启后租约恢复")
    job = job_repo.create({
        "project_id": pid, "capability": "video", "kind": "video_local",
        "execution_mode": "local", "provider_code": "mock",
        "model_code": adapters.MOCK_MODEL, "adapter_version": "mock",
        "prompt_snapshot": "演练 1", "params": {}, "input_refs": [],
    })
    job_repo.claim_next("drill-worker", 60)
    # claim 把作业领成 starting（worker 真正拉起子进程后才转 running）；
    # 对租约恢复来说两者一视同仁，recover_stale 的判据里两个状态都在。
    assert job_repo.status_of(job["id"]) in ("starting", "running")
    # 租约还在期内：recover_stale 不许动它——正常跑着的长任务不能被误杀
    recovered = job_repo.recover_stale()
    assert recovered == {"failed": 0, "cancelled": 0, "needs_attention": 0}, recovered
    ok("租约未过期时不误伤", f"recover_stale 返回 {recovered}，作业仍是 running")
    backdate_lease(job["id"])
    recovered = job_repo.recover_stale()
    row = job_repo.get(job["id"])
    assert row["status"] == "failed" and row["error_code"] == "worker_lost", dict(row)
    ok("租约过期后收成 failed(worker_lost)", f"status={row['status']}，错误码={row['error_code']}，可重试")
    # 清场：把这条收走，别影响后面演练的计数
    db_exec("DELETE FROM job WHERE id = ?", job["id"])

    # ---------- 演练 2：云端已取得请求 ID、租约过期 ----------
    print("[演练 2] 云端已取得供应商请求 ID、本地租约过期 → 待核对而不是自动重试")
    job = job_repo.create({
        "project_id": pid, "capability": "text", "kind": "text",
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "adapter_version": "test",
        "prompt_snapshot": "演练 2", "params": {}, "input_refs": [],
        "quantity": 0.002, "unit": "million_token",
        "unit_price_micros": 8_000_000, "cost_micros": 16_000, "currency": "CNY",
        "billable_status": "pending_reconciliation",
        "pricing_snapshot": {"source": "演练"}, "user_confirmed_cloud": True,
    })
    job_repo.claim_next("drill-worker", 60)
    db_exec("UPDATE job SET provider_request_id = 'req-drill-2' WHERE id = ?", job["id"])
    backdate_lease(job["id"])
    recovered = job_repo.recover_stale()
    row = job_repo.get(job["id"])
    assert row["status"] == "needs_attention" and row["error_code"] == "worker_lost", dict(row)
    assert row["cost_micros"] == 16_000 and row["billable_status"] == "pending_reconciliation"
    ok("挂 needs_attention，不判 failed", f"recover_stale 里 needs_attention={recovered['needs_attention']}；"
       "成本快照与待核对标记原样保留，系统不会替人再打一次供应商")
    db_exec("DELETE FROM job WHERE id = ?", job["id"])

    # ---------- 演练 3：云端结果未知、账单未确认 → 预估不冒充实际 ----------
    print("[演练 3] 云端结果未知、账单未确认：核对必须给实际金额，预估金额不被改写")
    job = job_repo.create({
        "project_id": pid, "capability": "text", "kind": "text",
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "adapter_version": "test",
        "prompt_snapshot": "演练 3", "params": {}, "input_refs": [],
        "quantity": 0.002, "unit": "million_token",
        "unit_price_micros": 8_000_000, "cost_micros": 16_000, "currency": "CNY",
        "billable_status": "pending_reconciliation",
        "pricing_snapshot": {"source": "演练"}, "user_confirmed_cloud": True,
    })
    job_repo.claim_next("drill-worker", 60)
    job_repo.needs_attention(job["id"], "drill-worker", "provider_timeout",
                             "结果未知", "云端调用超时", provider_request_id="req-drill-3")
    # 不给实际金额就想"确认成功"：拒
    r = client.post(f"/api/jobs/{job['id']}/reconcile",
                    json={"decision": "succeeded", "note": "没给金额"})
    assert r.status_code == 409 and "实际金额" in r.json()["detail"], r.text
    ok("不给实际金额拒核", f"409：{r.json()['detail']}")
    r = client.post(f"/api/jobs/{job['id']}/reconcile",
                    json={"decision": "succeeded", "actual_cost_micros": 12_345,
                          "note": "账单核对通过"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded" and body["billable_status"] == "known"
    assert body["actual_cost_micros"] == 12_345 and body["cost_micros"] == 16_000
    ok("核对后：实际金额入账，预估快照不动",
       f"status=succeeded，billable=known，actual=12345，预估 cost_micros 仍=16000")

    # ---------- 演练 4：产物为空/损坏，不许判成功 ----------
    print("[演练 4] 产物路径存在但文件为空或损坏：Job 不能误判成功")
    empty = _TMP / "empty.mp4"; empty.write_bytes(b"")
    junk = _TMP / "junk.mp4"; junk.write_bytes("这不是视频，只是些随机文本".encode("utf-8"))
    good = _TMP / "good.mp4"; good.write_bytes(b"\x00\x00\x00\x18ftypdemo\x00\x00\x00\x00")
    spec_empty = type("S", (), {"expected": [empty], "media": "video"})()
    spec_junk = type("S", (), {"expected": [junk], "media": "video"})()
    spec_good = type("S", (), {"expected": [good], "media": "video"})()
    try:
        runners.verify(spec_empty)
        raise AssertionError("空文件竟然过了 verify")
    except runners.RunnerUnavailable as exc:
        assert "产物是空的" in str(exc)
        ok("空文件被拒", f"RunnerUnavailable：{exc}")
    try:
        runners.verify(spec_junk)
        raise AssertionError("乱字节竟然过了 verify")
    except runners.RunnerUnavailable as exc:
        assert "不是可识别" in str(exc)
        ok("损坏文件被拒", f"RunnerUnavailable：{exc}")
    assert runners.verify(spec_good) == [good]
    ok("正常 ISO-BMFF 头放行", "ftyp 头校验通过，verify 返回产物路径")

    # ---------- 演练 5：重试链不覆盖历史、重试前先看账 ----------
    print("[演练 5] 重试：先看预览（会继承什么、可能再花多少），重试不覆盖历史")
    # 重新造一条失败的云端作业（演练 3 那条已核对成 succeeded，不该允许重试）
    original = job_repo.create({
        "project_id": pid, "capability": "text", "kind": "text",
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "adapter_version": "test",
        "prompt_snapshot": "演练 5：先核对再重试", "params": {}, "input_refs": [],
        "quantity": 0.002, "unit": "million_token",
        "unit_price_micros": 8_000_000, "cost_micros": 16_000, "currency": "CNY",
        "billable_status": "pending_reconciliation",
        "pricing_snapshot": {"source": "演练"}, "user_confirmed_cloud": True,
    })
    job_repo.claim_next("drill-worker", 60)
    job_repo.fail(original["id"], "drill-worker", "provider_auth", "模拟失败",
                  provider_request_id="req-drill-5")
    before = dict(job_repo.get(original["id"]))
    r = client.get(f"/api/jobs/{original['id']}/retry-preview")
    assert r.status_code == 200, r.text
    preview = r.json()
    ok("重试预览", f"chain_total_micros={preview['chain_total_micros']}，"
       f"chain_pending={preview['chain_pending']}，may_charge_again={preview['may_charge_again']}，"
       f"warning={preview['warning'] or '（无）'}")
    r = client.post(f"/api/jobs/{original['id']}/retry")
    assert r.status_code == 202, r.text
    new_id = r.json()["id"]
    after = dict(job_repo.get(original["id"]))
    for col in ("status", "error_code", "error_message", "cost_micros",
                "billable_status", "finished_at", "result_paths_json"):
        assert before[col] == after[col], f"原行被改动：{col}"
    new_row = job_repo.get(new_id)
    assert new_row["retry_of_job_id"] == original["id"]
    assert new_row["prompt_snapshot"] == before["prompt_snapshot"]
    assert new_row["params_json"] == before["params_json"]
    ok("重试落成新行", f"新作业 #{new_id}（retry_of=#{original['id']}），"
       "提示词与参数快照原样继承；原行状态、错误、成本、产物路径一个字段都没动")
    db_exec("DELETE FROM job WHERE id IN (?, ?)", new_id, original["id"])

    # ---------- 演练 6：重复提交幂等 ----------
    print("[演练 6] 重复点击提交：同一个幂等键返回同一条，不重复执行、不重复扣费")
    body = {
        "project_id": pid, "capability": "video", "prompt": "演练 6",
        "execution_mode": "local", "provider_code": "mock",
        "model_code": adapters.MOCK_MODEL, "idempotency_key": "drill:duplicate-1",
    }
    r1 = client.post("/api/jobs", json=body)
    assert r1.status_code == 202, r1.text
    r2 = client.post("/api/jobs", json=body)
    assert r2.status_code == 200, r2.text          # 幂等命中回 200，不是再建一条
    assert r2.json()["id"] == r1.json()["id"]
    n = sqlite3.connect(os.environ["FASTVIDEO_DB"]).execute(
        "SELECT COUNT(*) FROM job WHERE idempotency_key = 'drill:duplicate-1'").fetchone()[0]
    assert n == 1, f"同一个键落了 {n} 行"
    ok("幂等键命中", f"第二次回 200、作业号仍是 #{r1.json()['id']}，库里这个键只有 {n} 行")

    # ---------- 演练 7：worker 离线/积压告警只报事实 ----------
    print("[演练 7] worker 离线、队列积压、有待核对：告警报事实，不改任何作业状态")
    r = client.get("/api/worker/status")
    assert r.status_code == 200, r.text
    base = r.json()
    # 造积压：三条排队 + 一条待核对；把心跳拨老 = worker 离线
    for i in range(3):
        job_repo.create({
            "project_id": pid, "capability": "video", "kind": "video_local",
            "execution_mode": "local", "provider_code": "mock",
            "model_code": adapters.MOCK_MODEL, "adapter_version": "mock",
            "prompt_snapshot": f"演练 7-{i}", "params": {}, "input_refs": [],
        })
    stale = (datetime.now() - timedelta(seconds=600)).strftime("%Y-%m-%d %H:%M:%S")
    db_exec("UPDATE worker_state SET heartbeat_at = ?", stale)
    r = client.get("/api/worker/status")
    status = r.json()
    codes = [a["code"] for a in status["alerts"]]
    queued = status["queue"]["queued"]
    assert status["online"] is False, status
    assert queued >= 3, status
    ok("告警清单", f"online={status['online']}，queued={queued}，"
       f"alerts={codes}")
    # 告警不许改状态：排队的还在排队
    r = client.get("/api/jobs?status=queued")
    assert len(r.json()) >= 3
    ok("告警不改状态", "报警之后排队作业仍是 queued，没有谁被悄悄重试或判失败")

    print(f"\n七条演练全部通过（临时库：{os.environ['FASTVIDEO_DB']}，可整目录删除）")


if __name__ == "__main__":
    main()
