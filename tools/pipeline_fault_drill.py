"""产线故障演练（附件 §12.3 的十条，零成本、可重复）。

与 `tools/fault_drill.py` 同一套边界，这里只多说产线特有的两条：

1. **崩溃用"少调一个函数"来模拟**，不用真的杀进程。产线的恢复判据只有一条：
   库里存的是什么状态。所以"崩在某两步之间"＝把那两步拆开、中间不调推进；
   重启＝重新调 `advance_run()`。这样演练读到的是真实代码路径，而不是剧本。
2. **十条演练一条都不打真供应商**：本地作业走假适配器（只 sleep、写文本），
   云端语义（演练 9）用仓储层造数，`provider_request_id` 是编的字符串。
   假适配器只能证明代码路径与故障处理逻辑，**不能当作真实模型的生产验证证据**。

用法：app/backend/.venv/bin/python tools/pipeline_fault_drill.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "app" / "backend"
sys.path.insert(0, str(BACKEND))

# ↓ 环境必须先于任何项目 import：config 在导入时读死路径
_TMP = Path(tempfile.mkdtemp(prefix="fv-pdrill-"))
os.environ["FASTVIDEO_DB"] = str(_TMP / "drill.db")
os.environ["FASTVIDEO_FILES"] = str(_TMP / "files")
os.environ["FASTVIDEO_WORKSPACE"] = str(_TMP / "workspace")
os.environ["FASTVIDEO_JOBS"] = str(_TMP / "jobs")
os.environ["FASTVIDEO_ALLOW_MOCK"] = "1"
os.environ["FASTVIDEO_ADMIN_PASSWORD"] = "drill-only-not-a-real-secret"

import json                                       # noqa: E402
from datetime import datetime, timedelta          # noqa: E402

import adapters                                   # noqa: E402
import pipeline                                   # noqa: E402
import services                                   # noqa: E402
import worker                                     # noqa: E402
from bootstrap import ensure_admin                # noqa: E402
from db import get_conn, transaction              # noqa: E402
from migrate import migrate                       # noqa: E402
from repositories.content import episodes, shots  # noqa: E402
from repositories.jobs import jobs as job_repo    # noqa: E402
from repositories.pipeline import pipeline_repo   # noqa: E402
from repositories.projects import projects as project_repo  # noqa: E402
from repositories.reviews import reviews          # noqa: E402
from repositories.users import users as user_repo  # noqa: E402

PASSED: list[str] = []
FAST = {"sleep": 0.01}

# 演练用的流程：读项目 → 逐镜关键帧 → 逐镜产物检查 → 逐镜人工验收。
# 不带 human_gate（那要先有正文），也不带成片准备（阶段 D）；
# 该有的三类节点（内部检查、作业、审核）都在，够把十条演练走完。
DRILL_TEMPLATE = {"nodes": [
    {"key": "read_project", "type": "project_input", "depends_on": []},
    {"key": "keyframe", "type": "image_generate", "depends_on": ["read_project"],
     "fan_out": "shot", "params": FAST},
    {"key": "check", "type": "artifact_validate", "depends_on": ["keyframe"],
     "fan_out": "shot"},
    {"key": "review", "type": "image_review", "depends_on": ["check"],
     "fan_out": "shot"},
]}


def ok(title: str, detail: str) -> None:
    """记一条演练结论：只报观测到的事实。"""
    print(f"  → {title}\n    {detail}")
    PASSED.append(title)


def db_exec(sql: str, *args) -> None:
    """演练里的"时间机器"：改租约、改作业状态都用它。只打临时库。"""
    with get_conn() as conn:
        conn.execute(sql, args)


def db_one(sql: str, *args):
    with get_conn() as conn:
        return conn.execute(sql, args).fetchone()


def backdate_lease(job_id: int, seconds: int = 600) -> None:
    """把租约拨回过去——等效于"worker 死了很久，没人续租"。"""
    past = (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    db_exec("UPDATE job SET lease_until = ? WHERE id = ?", past, job_id)


def _mock_adapter(capability: str, backend: str):
    """给 mock 造一个"登记过"的外壳：`resolve_provider` 不认没登记的 provider。"""
    return adapters.Adapter(
        code="mock", label="假适配器（演练壳）", capability=capability,
        backend=backend, source="tools/pipeline_fault_drill.py",
        params_source="adapters.MOCK_PARAMS",
        models=(adapters.ModelSpec(code=adapters.MOCK_MODEL, label="mock-1",
                                   params=adapters.MOCK_PARAMS),))


def use_mock_route() -> None:
    """把图像与视频的默认轨换成假适配器。库是一次性的，用完不还原。"""
    real_find = adapters.find

    def _find(capability, backend, provider):
        if (provider or "").strip().lower() == "mock":
            return _mock_adapter(capability, backend)
        return real_find(capability, backend, provider)

    adapters.find = _find
    with get_conn() as conn:
        conn.execute("UPDATE provider_config SET is_default = 0 "
                     "WHERE capability IN ('image','video')")
        for capability in ("image", "video"):
            conn.execute(
                """UPDATE provider_config
                      SET provider = 'mock', model = ?, enabled = 1,
                          is_default = 1, key_env = ''
                    WHERE capability = ? AND backend = 'local'""",
                (adapters.MOCK_MODEL, capability))


def setup(admin_id: int, n_shots: int = 2) -> dict:
    """一套真实输入：项目 → 集 → 镜头。每条演练各用一套，互不干扰。"""
    pid = project_repo.create(f"产线演练_{uuid.uuid4().hex[:6]}",
                              "pipeline_fault_drill 用，可整库丢弃", [], admin_id)["id"]
    eid = episodes.create(pid, "第 1 集")["id"]
    sids = [shots.create(eid, f"镜 {i + 1}", f"提示词 {i + 1}", 5.0, [])["id"]
            for i in range(n_shots)]
    return {"project_id": pid, "episode_id": eid, "shot_ids": sids}


def template_id() -> int:
    row = pipeline_repo.ensure_template(
        name=f"演练流程_{uuid.uuid4().hex[:6]}", version=1, description="演练用",
        definition=DRILL_TEMPLATE, builtin=False)
    return row["id"]


def clean_queue() -> int:
    """把上一条演练留下的活收掉：演练之间不许互相踩。

    不收的话 `claim_next()` 会领到别人那条，"worker 被强制终止"这一条
    就会去拨一条根本没被领取的作业的租约，看到的自然是假的。
    """
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE job SET status = 'cancelled',
                      finished_at = datetime('now','localtime'),
                      error_code = 'drill_cleanup'
                WHERE status IN ('queued','starting','running','cancel_requested')""")
        return cur.rowcount


def new_run(admin_id: int, n_shots: int = 2) -> dict:
    clean_queue()
    setup_ = setup(admin_id, n_shots)
    view = pipeline.create_run(setup_["project_id"], setup_["episode_id"],
                               template_id(), admin_id)
    return {**setup_, "run_id": view["id"]}


def nodes(run_id: int) -> dict:
    return {n["node_key"]: n for n in pipeline.run_view(run_id)["nodes"]}


def sign_artifact(job_id: int, ctx: dict, admin_id: int,
                  decision: str = "approved") -> None:
    """给一条作业的产物签字——产线的验收节点等的就是它。"""
    job = job_repo.get(job_id)
    reviews.upsert_sign(project_id=ctx["project_id"], target_kind=reviews.JOB,
                        target_id=job_id, gate_id=None,
                        base_version=services.artifact_version(job),
                        anchor="artifact", decision=decision, note="演练签字",
                        user_id=admin_id)


def drain(limit: int = 20) -> int:
    """把队列跑空，返回跑了几条。"""
    ran = 0
    for _ in range(limit):
        if worker.run_once("drill-worker") is None:
            break
        ran += 1
    return ran


def job_count(run_id: int) -> int:
    return db_one("SELECT COUNT(*) AS n FROM job WHERE idempotency_key LIKE ?",
                  f"pl-{run_id}-%")["n"]


def project_jobs(project_id: int) -> int:
    """这个项目名下一共几条作业（含重试行）。"""
    return db_one("SELECT COUNT(*) AS n FROM job WHERE project_id = ?",
                  project_id)["n"]


def main() -> int:
    migrate(verbose=False)                       # 临时库要先补到最新，表才存在
    ensure_admin(verbose=False)
    use_mock_route()
    pipeline.ensure_builtin_templates()
    admin = user_repo.get_by_username("superadmin")
    uid = admin["id"]

    # ---------- 演练 1：产线创建后服务崩溃 ----------
    print("[演练 1] 运行与节点已落库、还没推进过就崩了 → 重启后接着走")
    ctx = setup(uid)
    tid = template_id()
    # 崩溃点：create_run 的事务已提交（运行＋节点在库里），进程在推进前没了。
    # 这里照它的写库步骤手工造出那个状态，不靠"少调一个函数"糊弄过去。
    snapshot = pipeline.build_input_snapshot(ctx["project_id"], ctx["episode_id"])
    expanded = pipeline.expand_nodes(DRILL_TEMPLATE, snapshot)
    with transaction():
        run = pipeline_repo.create_run(
            project_id=ctx["project_id"], episode_id=ctx["episode_id"],
            template_id=tid, template_version=1, input_snapshot=snapshot,
            idempotency_key=None, created_by=uid,
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        pipeline_repo.create_nodes(run["id"], expanded)
    run_id = run["id"]
    before = nodes(run_id)
    assert all(n["stored_status"] == "pending" for n in before.values()), "起点就该全 pending"
    pipeline.tick()                              # ＝ worker 重启后的第一轮
    after = nodes(run_id)
    assert after["read_project"]["status"] == "completed"
    assert len(after) == len(before) == len(expanded)
    first_key = f"keyframe#s{ctx['shot_ids'][0]}"
    ok("重启后从库里的状态接着推进",
       f"{len(before)} 个节点一个没多一个没少；read_project={after['read_project']['status']}，"
       f"关键帧节点已派作业 job_id={after[first_key]['job_id']}")

    # ---------- 演练 2：节点创建后、Job 提交前崩溃 ----------
    print("[演练 2] 节点已就绪、作业还没提交就崩了 → 重启后只提交一条")
    ctx = new_run(uid, n_shots=1)
    run_id = ctx["run_id"]
    sid = ctx["shot_ids"][0]
    node = nodes(run_id)[f"keyframe#s{sid}"]
    assert node["job_id"], "这一格本该已经派了作业"
    # 崩溃点回退：把节点放回 pending、摘掉关联，等效于"提交那一步没发生"
    db_exec("UPDATE pipeline_node_run SET status = 'pending', job_id = NULL, "
            "started_at = NULL WHERE id = ?", node["id"])
    db_exec("DELETE FROM job WHERE id = ?", node["job_id"])
    assert job_count(run_id) == 0
    pipeline.advance_run(run_id)
    got = nodes(run_id)[f"keyframe#s{sid}"]
    assert got["job_id"] and job_count(run_id) == 1, job_count(run_id)
    pipeline.advance_run(run_id)                 # 再推一轮，验重复提交
    assert job_count(run_id) == 1, "重启后多提交了一条作业"
    ok("补提交且只提交一条",
       f"节点 job_id={got['job_id']}，幂等键={job_repo.get(got['job_id'])['idempotency_key']}；"
       f"连推两轮库里仍只有 {job_count(run_id)} 条产线作业")

    # ---------- 演练 3：Job 创建后、节点关联前崩溃 ----------
    print("[演练 3] 作业已落库、节点还不知道 → 重启后认回那一条，不建第二条")
    ctx = new_run(uid, n_shots=1)
    run_id = ctx["run_id"]
    sid = ctx["shot_ids"][0]
    node = nodes(run_id)[f"keyframe#s{sid}"]
    orphan = node["job_id"]
    # 崩溃点：作业行在（键是确定性算出来的），节点被放回 pending 且摘掉关联
    db_exec("UPDATE pipeline_node_run SET status = 'pending', job_id = NULL, "
            "started_at = NULL WHERE id = ?", node["id"])
    assert job_repo.by_key(job_repo.get(orphan)["idempotency_key"])["id"] == orphan
    pipeline.advance_run(run_id)
    got = nodes(run_id)[f"keyframe#s{sid}"]
    assert got["job_id"] == orphan, f"没有认回原作业：{got['job_id']} != {orphan}"
    assert job_count(run_id) == 1
    events = [e for e in pipeline.run_view(run_id)["events"]
              if e["event_type"] == "job_submitted" and e["payload"].get("reused")]
    ok("按幂等键认回原作业",
       f"节点重新指向 #{orphan}，产线作业仍是 {job_count(run_id)} 条；"
       f"事件里记了 reused=true（{len(events)} 条）")

    # ---------- 演练 4：worker 执行时被强制终止 ----------
    print("[演练 4] 作业跑一半 worker 没了 → 租约恢复判失败，产线不自动重试")
    ctx = new_run(uid, n_shots=1)
    run_id = ctx["run_id"]
    sid = ctx["shot_ids"][0]
    node = nodes(run_id)[f"keyframe#s{sid}"]
    job_repo.claim_next("drill-worker", 60)
    backdate_lease(node["job_id"])
    recovered = job_repo.recover_stale()
    row = job_repo.get(node["job_id"])
    assert row["status"] == "failed" and row["error_code"] == "worker_lost", dict(row)
    total = job_count(run_id)
    view = pipeline.advance_run(run_id)
    got = nodes(run_id)[f"keyframe#s{sid}"]
    assert got["status"] == "failed" and got["error_code"] == "worker_lost"
    assert job_count(run_id) == total, "产线替人重试了"
    ok("恢复成 failed(worker_lost)，节点跟着失败",
       f"recover_stale={recovered}；节点 error_code={got['error_code']}，"
       f"运行状态={view['status']}，作业数仍是 {total}（没有自动重试）")

    # ---------- 演练 5：Job 成功后产线还没更新 ----------
    print("[演练 5] 作业已成功、节点还停在执行中 → 重启后按真实状态收")
    ctx = new_run(uid, n_shots=2)
    run_id = ctx["run_id"]
    drain()                                      # 两镜关键帧都跑完
    # 崩溃点：作业成功了，但同步那一步没跑过（节点仍是 running）
    stuck = [(k, n["job_id"]) for k, n in nodes(run_id).items()
             if k.startswith("keyframe") and n["stored_status"] == "running"]
    assert len(stuck) == 2, stuck
    assert all(job_repo.get(j)["status"] == "succeeded" for _, j in stuck)
    view = pipeline.advance_run(run_id)
    after = nodes(run_id)
    assert all(after[k]["status"] == "completed" for k, _ in stuck)
    # 收完立刻放出下游：产物检查是内部节点，同一轮就该做完
    assert all(after[f"check#s{s}"]["status"] == "completed" for s in ctx["shot_ids"])
    ok("按 Job 的真实状态收节点，并放出下游",
       f"两格 keyframe 由 running → completed；产物检查同轮完成，"
       f"运行状态={view['status']}，验收节点={after[f'review#s{ctx['shot_ids'][0]}']['status']}")

    # ---------- 演练 6：节点失败后单独重试 ----------
    print("[演练 6] 一镜失败，只重试那一镜；另一镜的作业一条都不动")
    ctx = new_run(uid, n_shots=2)
    run_id = ctx["run_id"]
    good, bad = ctx["shot_ids"]
    db_exec("UPDATE job SET params_json = ? WHERE id = ?",
            json.dumps({"sleep": 0.01, "fail": True}),
            nodes(run_id)[f"keyframe#s{bad}"]["job_id"])
    drain()
    view = pipeline.advance_run(run_id)
    before = nodes(run_id)
    assert before[f"keyframe#s{bad}"]["status"] == "failed"
    assert before[f"keyframe#s{good}"]["status"] == "completed"
    old_job = before[f"keyframe#s{bad}"]["job_id"]
    good_job = before[f"keyframe#s{good}"]["job_id"]
    # 重试走 Job 层那套语义：新行的幂等键是 `retry-<uuid>`，不是产线那个确定性键。
    # 所以这里数"这个项目一共几条作业"，而不是数产线键。
    total = project_jobs(ctx["project_id"])
    pipeline.retry_node(before[f"keyframe#s{bad}"]["id"], uid)
    after = nodes(run_id)
    new_job = after[f"keyframe#s{bad}"]["job_id"]
    assert new_job != old_job
    assert project_jobs(ctx["project_id"]) == total + 1, project_jobs(ctx["project_id"])
    assert job_count(run_id) == total, "调度器又提交了一条（一格两条作业）"
    assert after[f"keyframe#s{good}"]["job_id"] == good_job
    assert after[f"keyframe#s{good}"]["retry_count"] == 0
    assert job_repo.get(old_job)["superseded_by_job_id"] == new_job
    assert job_repo.get(new_job)["retry_of_job_id"] == old_job
    ok("只重试失败那一格",
       f"新作业 #{new_job}（retry_of=#{old_job}、attempt="
       f"{job_repo.get(new_job)['attempt']}），旧行原样留着并被指过去；"
       f"好那一镜的作业仍是 #{good_job}、retry_count=0；"
       f"项目作业 {total} → {total + 1}，其中产线键仍 {job_count(run_id)} 条")

    # ---------- 演练 7：等待审核时重启 ----------
    print("[演练 7] 卡在等签字时重启 → 仍在等签字，签字之后才继续")
    ctx = new_run(uid, n_shots=1)
    run_id = ctx["run_id"]
    sid = ctx["shot_ids"][0]
    drain()
    view = pipeline.advance_run(run_id)
    assert nodes(run_id)[f"review#s{sid}"]["status"] == "waiting_review"
    total = job_count(run_id)
    for _ in range(3):                           # ＝重启三次
        pipeline.advance_run(run_id)
    view = pipeline.run_view(run_id)
    assert nodes(run_id)[f"review#s{sid}"]["status"] == "waiting_review"
    assert job_count(run_id) == total, "等签字期间又派了作业"
    ok("重启不改审核状态、不派新活",
       f"三轮推进后仍是 waiting_review，运行状态={view['status']}，作业数 {total} 未变")
    sign_artifact(nodes(run_id)[f"keyframe#s{sid}"]["job_id"], ctx, uid)
    view = pipeline.advance_run(run_id)
    assert nodes(run_id)[f"review#s{sid}"]["status"] == "completed"
    assert view["status"] == "completed" and view["finished_at"]
    ok("签字之后同一轮就收尾",
       f"review → completed，运行 {view['status']}（{view['progress']['done']}/"
       f"{view['progress']['total']}），finished_at={view['finished_at']}")

    # ---------- 演练 8：暂停后 worker 仍有作业在跑 ----------
    print("[演练 8] 暂停时作业还在跑 → 不强杀；跑完的结果照实记，但不派新活")
    ctx = new_run(uid, n_shots=1)
    run_id = ctx["run_id"]
    sid = ctx["shot_ids"][0]
    node = nodes(run_id)[f"keyframe#s{sid}"]
    claimed = job_repo.claim_next("drill-worker", 60)   # 领起来＝正在跑
    assert claimed is not None and claimed["id"] == node["job_id"], claimed
    pipeline.pause_run(run_id)
    assert job_repo.get(node["job_id"])["status"] in ("starting", "running")
    total = job_count(run_id)
    pipeline.advance_run(run_id)                 # 暂停中推进：不许派新活
    assert job_count(run_id) == total
    worker.process(claimed, "drill-worker")      # worker 手上这条照样跑完（不强杀）
    view = pipeline.advance_run(run_id)
    got = nodes(run_id)[f"keyframe#s{sid}"]
    assert got["status"] == "completed", got["status"]
    assert job_count(run_id) == total
    ok("暂停不强杀、结果照实记、不派新活",
       f"暂停期间作业由 running 跑到 succeeded，节点收成 {got['status']}；"
       f"下游 check={nodes(run_id)[f'check#s{sid}']['status']}（暂停 ⇒ 没往下派），"
       f"运行状态={view['status']}，作业数 {total} 未变")
    view = pipeline.resume_run(run_id)
    assert nodes(run_id)[f"check#s{sid}"]["status"] == "completed"
    ok("恢复之后接着往下走",
       f"resume 那一轮就把产物检查做完了：check=completed，运行状态={view['status']}")

    # ---------- 演练 9：云端请求 ID 已返回、本地租约过期 ----------
    print("[演练 9] 云端已取得请求 ID、租约过期 → 待核对，产线不替人再打一次")
    ctx = new_run(uid, n_shots=1)
    run_id = ctx["run_id"]
    sid = ctx["shot_ids"][0]
    node = nodes(run_id)[f"keyframe#s{sid}"]
    # 造一条"云端作业"的账：请求 ID 已回来、租约过期。金额与请求 ID 都是编的。
    db_exec("""UPDATE job SET execution_mode = 'cloud', provider_code = 'deepseek',
                      model_code = 'deepseek-flash', provider_request_id = 'req-drill-9',
                      quantity = 0.002, unit = 'million_token',
                      unit_price_micros = 8000000, cost_micros = 16000,
                      currency = 'CNY', billable_status = 'pending_reconciliation',
                      user_confirmed_cloud = 1
                WHERE id = ?""", node["job_id"])
    job_repo.claim_next("drill-worker", 60)
    backdate_lease(node["job_id"])
    recovered = job_repo.recover_stale()
    row = job_repo.get(node["job_id"])
    assert row["status"] == "needs_attention", dict(row)
    total = job_count(run_id)
    view = pipeline.advance_run(run_id)
    got = nodes(run_id)[f"keyframe#s{sid}"]
    assert got["status"] == "attention" and got["error_code"] == "job_needs_attention"
    assert job_count(run_id) == total, "产线替人重打了供应商"
    ok("节点摆成 attention，不自动重试",
       f"recover_stale 里 needs_attention={recovered['needs_attention']}；"
       f"请求 ID 仍={row['provider_request_id']}、预估 cost_micros={row['cost_micros']}、"
       f"billable={row['billable_status']}；节点 error_message=「{got['error_message'][:40]}…」，"
       f"运行状态={view['status']}，作业数 {total} 未变")

    # ---------- 演练 10：同一节点被重复触发 ----------
    print("[演练 10] 两个线程同时推进同一条运行 → 一格只有一条作业")
    ctx = new_run(uid, n_shots=2)
    run_id = ctx["run_id"]
    # 起点：节点已经派过作业；把它们放回 pending，模拟"两边同时看到待派发"
    with get_conn() as conn:
        conn.execute("""UPDATE pipeline_node_run
                           SET status = 'pending', started_at = NULL
                         WHERE run_id = ? AND node_type = 'image_generate'""",
                     (run_id,))
    errors: list[BaseException] = []

    def _advance():
        try:
            pipeline.advance_run(run_id)
        except BaseException as exc:            # 演练要把异常原样带出来
            errors.append(exc)

    threads = [threading.Thread(target=_advance) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    after = nodes(run_id)
    keys = [f"keyframe#s{s}" for s in ctx["shot_ids"]]
    # 每一格仍然只认一条作业；两条线程不可能各派一条
    for key in keys:
        n = db_one("""SELECT COUNT(*) AS n FROM job
                       WHERE idempotency_key = ?""",
                   f"pl-{run_id}-{key}-a1")["n"]
        assert n == 1, f"{key} 落了 {n} 条作业"
        assert after[key]["job_id"], key
    node_rows = db_one("SELECT COUNT(*) AS n FROM pipeline_node_run WHERE run_id = ?",
                       run_id)["n"]
    ok("并发推进不会重复派活",
       f"两个线程各调一次 advance_run，{len(keys)} 格每格库里只有 1 条作业"
       f"（幂等键 pl-{run_id}-<node>-a1 唯一）；节点行数 {node_rows} 未变。"
       f"挡住重复的机制是 try_begin 的 rowcount（第二个调用拿到 0 就直接退出），"
       f"这一条只报观测到的结果，不去猜哪一个线程赢了")

    print(f"\n十条演练全部通过（临时库：{os.environ['FASTVIDEO_DB']}，可整目录删除）")
    print("提醒：以上全部走假适配器，只证明代码路径与故障处理逻辑，"
          "不是真实模型的生产验证证据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
