"""智能体中心（0041）的后端用例。

三条判据，都是《智能体管理中心》说明书 §10 那批验收标准的"可红"版本：

* **白名单是结构不是提示**。不在表里的工具码、表里没声明的参数键一律拒——
  拒的是"多出来的东西"，所以攻击面不是被过滤掉，是不存在。
* **审批绑参数**。签字落 `review_item` 时绑的是那一步的参数指纹；
  参数被动过（指纹变了）旧批准就不算数，宁可停在等人。
* **失败不粉饰**。计划读不出来、产物是空的、步骤停在执行中却没有作业指针——
  全部按失败处理并留下人看得懂的一句话，绝不写成"已完成"。

模型这一腿走本地假 HTTP（与 `test_doc_ai.py`／`test_cloud.py` 同一套思路）：
真队列、真 worker、真 `cloud_run.py` 子进程，只有供应商端是假的。
**假接口跑通不等于真模型验证过**，真实调用的结论单独记在交付报告里。
"""
import json
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import agents
import agent_tools
import engines
import worker
from db import get_conn
from repositories.agents import (STEP_AWAITING, STEP_FAILED, STEP_SUCCEEDED,
                                 agent_runs)
from repositories.content import assets
from repositories.jobs import jobs as job_repo

SECRET = "sk-agents-test-secret"
GOAL = "盘点这一集缺哪些资产，并给缺的角色补一段人物履历"

PLAN_READ = json.dumps([
    {"tool": "episode_preflight", "args": {"episode_id": 0}, "why": "先看缺什么"},
])
PLAN_WRITE = json.dumps([
    {"tool": "asset_register",
     "args": {"kind": "character", "name": "智能体登记的角色"}, "why": "补上缺的角色"},
])


class _Chat(BaseHTTPRequestHandler):
    """只回一个 chat 补全。内容由 server.reply 决定，状态码由 server.status 决定。"""

    def log_message(self, *args):
        return

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        self.server.calls.append(body)
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            self._json(401, {"error": {"message": "bad key " + SECRET}})
            return
        if self.server.status != 200:
            self._json(self.server.status, {"error": {"message": "限流 " + SECRET}})
            return
        self._json(200, {
            "id": "chatcmpl-agents-1", "model": "deepseek-flash",
            "choices": [{"message": {"content": self.server.reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12,
                      "total_tokens": 52},
        })


@pytest.fixture
def plan_server(monkeypatch, provider_state):
    """文本默认轨＝云端 deepseek ＋ 本地假接口；用例可改 server.reply 与 server.status。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Chat)
    server.reply = PLAN_READ
    server.status = 200
    server.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("DEEPSEEK_BASE_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with get_conn() as conn:
        conn.execute("UPDATE provider_config SET is_default = 0 WHERE capability = 'text'")
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'deepseek', model = 'deepseek-flash',
                      enabled = 1, is_default = 1, key_env = 'DEEPSEEK_API_KEY'
                WHERE capability = 'text' AND backend = 'cloud'""")
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture(autouse=True)
def _teardown(app):
    """每条用例跑完把自己留下的活收掉。

    这一族会往**那一个队列**里排排队中的作业，也会留下没结束的运行。
    同目录其它用例（看板的数字、worker 告警）读的正是同一张 `job` 表，
    不收掉就会把它们打红——2026-11-06 全量跑就是这样红过两条，
    单独跑又全绿，属于最费时间的那类"顺序依赖"。
    """
    yield
    # 步骤真的登记过资产时那些行会留在同一个项目里，而后面有用例
    # 按"这个项目恰好一个资产"来核对看板数字。先把自己建的资产撤掉，
    # 再收运行——撤的依据是步骤结果里那个 id，不是"名字像测试数据"。
    with get_conn() as conn:
        for row in conn.execute("SELECT result_json FROM agent_step "
                                "WHERE result_json LIKE '%asset_id%'").fetchall():
            try:
                payload = json.loads(row["result_json"] or "{}")
            except ValueError:
                continue
            if payload.get("asset_id"):
                assets.delete(int(payload["asset_id"]))
        conn.execute("""UPDATE job SET status = 'cancelled',
                               finished_at = datetime('now','localtime'),
                               error_code = 'agents_teardown'
                        WHERE status IN ('queued','starting','running',
                                         'cancel_requested','needs_attention')""")
        conn.execute("""UPDATE agent_run SET cancelled = 1,
                               cancel_reason = '用例收尾',
                               finished_at = COALESCE(finished_at, datetime('now','localtime'))
                        WHERE finished_at IS NULL""")
        conn.execute("""UPDATE agent_step SET status = 'cancelled'
                        WHERE status IN ('pending','awaiting_approval','approved','running')""")
        # `worker.run_once()` 会写一行带心跳的 worker 记录，而 `worker_status()`
        # 读的是"最近 N 秒内有心跳的节点"。不收掉的话，后面那条
        # "离线且有排队"的用例会读到本轮留下的假在线（2026-11-06 真撞过）。
        conn.execute("""UPDATE worker_node
                      SET heartbeat_at = datetime('now','localtime','-1 hour'),
                          current_job_id = NULL""")
        conn.execute("""UPDATE worker_state
                      SET heartbeat_at = datetime('now','localtime','-1 hour')""")


@pytest.fixture
def ag_scene(people):
    """这一族专用的项目＋分集。

    为什么不用 `ag_scene`：计划作业是**云端计费**的，落进那个共享项目就会把
    它的成本账从空变成有数字，而 `test_dashboard.py` 有一条用例正是按
    "那个项目还没有一笔账单"来核对看板（2026-11-06 全量跑红在那儿）。
    自己的项目自己用，别靠收尾把账单抹掉——账那一腿不许测试替人删。
    """
    from repositories.content import episodes as episode_repo
    from repositories.projects import projects as project_repo
    pid = project_repo.create(f"智能体_{uuid.uuid4().hex[:6]}", "", [],
                              people["admin"]["id"])["id"]
    eid = episode_repo.create(pid, "第 1 集")["id"]
    return {"project_id": pid, "episode_id": eid}


def _fix_episode(raw: str, episode_id: int) -> str:
    """把计划里的占位集号换成这一条用例真实的那个集 id。"""
    return raw.replace('"episode_id": 0', f'"episode_id": {episode_id}')


def _start(client, ag_scene, **kw):
    body = {"project_id": ag_scene["project_id"], "episode_id": ag_scene["episode_id"],
            "goal": GOAL, "scene_key": "", "user_confirmed_cloud": True}
    body.update(kw)
    resp = client.post("/api/agent/runs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["run"]


def _add_step(run_id, tool, args, seq=1):
    clean = agent_tools.validate(tool, args)
    return agent_runs.add_step(
        run_id, seq, tool_code=tool, args=clean,
        args_hash=agent_tools.args_hash(tool, clean),
        risk=agent_tools.risk_of(tool))


# ==================== 白名单：结构性拒绝 ====================
def test_白名单外的工具码一律拒():
    with pytest.raises(agent_tools.ToolUnknown):
        agent_tools.validate("run_sql", {"sql": "select 1"})
    assert "run_sql" not in agent_tools.TOOLS


def test_多一个没声明的参数键就拒():
    """这条就是「禁止任意 SQL／Shell／Python」在参数层的落法。

    反证：把 `agent_tools.validate()` 里那句 unknown 判断删掉，三条都会绿——
    那正是最坏的那种"绿着但已经不安全"。
    """
    for extra in ({"sql": "drop table job"}, {"cmd": "rm -rf /"}, {"path": "/etc/passwd"}):
        args = {"episode_id": 1}
        args.update(extra)
        with pytest.raises(agent_tools.ToolArgsInvalid):
            agent_tools.validate("episode_preflight", args)


def test_枚举参数只认登记过的取值():
    with pytest.raises(agent_tools.ToolArgsInvalid):
        agent_tools.validate("asset_register", {"kind": "spaceship", "name": "一艘船"})
    assert agent_tools.validate(
        "asset_register", {"kind": "character", "name": "燧人"})["kind"] == "character"


def test_参数指纹与顺序无关但改一个字就变():
    a = agent_tools.args_hash("asset_register", {"kind": "character", "name": "燧人"})
    b = agent_tools.args_hash("asset_register", {"name": "燧人", "kind": "character"})
    assert a == b
    assert a != agent_tools.args_hash(
        "asset_register", {"kind": "character", "name": "农人"})


def test_费用那句话从清单现读不抄数字():
    assert "输出 token" in agent_tools.cost_note(
        "text", "deepseek", "deepseek-flash", "cloud")
    assert "不计费" in agent_tools.cost_note("text", "ollama", "qwen3:8b", "local")
    assert "不在适配器清单" in agent_tools.cost_note("text", "nope", "nope", "cloud")


# ==================== 计划解析：两处宽容两处不宽容 ====================
def test_计划被模型包一层说明也读得出来():
    raw = "好的，下面是计划：\n```json\n" + PLAN_WRITE + "\n```"
    steps = agents.parse_plan(raw)
    assert steps[0]["risk"] == "formal" and steps[0]["seq"] == 1


def test_计划坏在任何一处都整体失败():
    cases = [
        ("[]", "空数组"), ("这不是 JSON", "读不出数组"),
        (json.dumps([{"tool": "drop_database"}]), "未知工具"),
        (json.dumps([{"tool": "episode_preflight",
                      "args": {"episode_id": 1, "sql": "x"}}]), "多余参数"),
        (json.dumps([{"tool": "episode_preflight", "args": {"episode_id": i}}
                     for i in range(1, agents.MAX_PLAN_STEPS + 2)]), "超步数"),
    ]
    for raw, why in cases:
        with pytest.raises(agents.AgentInvalid, match=".+"):
            agents.parse_plan(raw)


def test_计划提示词里没有正文():
    """说明书 §2「云端发送内容需最小化」的可检查版本。

    把正文塞进计划提示词＝在没有逐次授权的情况下把整部稿子送出去。
    """
    prompt = agents.plan_prompt("只做一件事", None, {"shots": 12})
    assert "燧人蹲在洞口" not in prompt
    assert "episode_preflight" in prompt          # 工具清单必须给模型看
    assert "只做一件事" in prompt


# ==================== 发起：授权、成本单与幂等 ====================
def test_云端算计划要人点头也要有计价单(as_role, plan_server, ag_scene):
    c = as_role("admin")
    resp = c.post("/api/agent/runs", json={
        "project_id": ag_scene["project_id"], "goal": GOAL, "scene_key": ""})
    assert resp.status_code == 422
    assert "确认" in resp.json()["detail"]
    ok = c.post("/api/agent/runs", json={
        "project_id": ag_scene["project_id"], "goal": GOAL, "scene_key": "",
        "user_confirmed_cloud": True})
    assert ok.status_code == 201, ok.text
    job = job_repo.get(ok.json()["plan_job_id"])
    # 确认单不是走形式：它把单价、数量、价格来源与"谁点的"绑在一起
    assert job["billable_status"] == "pending_reconciliation"
    assert job["cost_micros"] > 0
    snapshot = json.loads(job["pricing_snapshot_json"])
    assert snapshot["source"] and snapshot["confirmed_by"]


def test_发起只多一条排队作业且运行停在算计划(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    job = job_repo.get(run["plan_job_id"])
    assert job["status"] == "queued" and job["capability"] == "text"
    assert c.get(f"/api/agent/runs/{run['id']}").json()["status"] == "planning"


def test_同一个幂等键不排第二条(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    body = {"project_id": ag_scene["project_id"], "goal": GOAL, "scene_key": "",
            "user_confirmed_cloud": True, "idempotency_key": "ag-test-key-1"}
    first = c.post("/api/agent/runs", json=body).json()["run"]["id"]
    again = c.post("/api/agent/runs", json=body)
    assert again.json()["reused"] is True and again.json()["run"]["id"] == first
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM agent_run "
                            "WHERE idempotency_key = ?",
                            ("ag-test-key-1",)).fetchone()["n"] == 1


def test_目标太短就不排一条注定没用的作业(as_role, plan_server, ag_scene):
    c = as_role("admin")
    assert c.post("/api/agent/runs", json={
        "project_id": ag_scene["project_id"], "goal": "做",
        "user_confirmed_cloud": True}).status_code == 422


# ==================== 真队列里的计划闭环 ====================
def test_计划回来落成步骤(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    plan_server.reply = _fix_episode(PLAN_READ, ag_scene["episode_id"])
    run = _start(c, ag_scene)
    assert worker.run_once("w-plan") == run["plan_job_id"]
    agents.tick()
    detail = c.get(f"/api/agent/runs/{run['id']}").json()
    assert detail["steps"][0]["tool_code"] == "episode_preflight"
    assert detail["plan_job"]["status"] == "succeeded"
    kinds = [e["event_type"] for e in detail["events"]]
    assert "plan_ready" in kinds and "run_created" in kinds


def test_计划不是JSON时运行失败且不留下假步骤(as_role, clean_queue, plan_server, ag_scene):
    """产物读得出来但不是计划 ⇒ 失败，一步都不建。

    反证：把 `_ingest_plan()` 里那句 `_fail_run(...)` 换成 `return`，
    运行会停在"在算计划"永远不动——那种挂法比失败更难查。
    """
    c = as_role("admin")
    plan_server.reply = "我想我可以先看看有什么资产，然后再决定。"
    run = _start(c, ag_scene)
    assert worker.run_once("w-plan") == run["plan_job_id"]
    agents.tick()
    detail = c.get(f"/api/agent/runs/{run['id']}").json()
    assert detail["status"] == "failed" and detail["steps"] == []
    assert detail["failure"]["code"] == "plan_unparsable"


def test_云端供应商失败时运行失败并带原话(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    plan_server.status = 429
    run = _start(c, ag_scene)
    assert worker.run_once("w-429") == run["plan_job_id"]
    agents.tick()
    detail = c.get(f"/api/agent/runs/{run['id']}").json()
    assert detail["status"] == "failed"
    assert detail["failure"]["message"]
    # 密钥值不许被带进失败原因（cloud_run 那侧脱敏，这里验它没被绕过）
    assert SECRET not in json.dumps(detail, ensure_ascii=False)


# ==================== 状态机 ====================
def test_只读步骤当场做完_写步骤停在等人批(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    _add_step(run["id"], "episode_preflight", {"episode_id": ag_scene["episode_id"]}, 1)
    _add_step(run["id"], "asset_register",
              {"kind": "character", "name": "批了才建的角色"}, 2)
    agents.tick()
    steps = agent_runs.steps(run["id"])
    assert steps[0]["status"] == STEP_SUCCEEDED and steps[0]["result_json"]
    assert steps[1]["status"] == STEP_AWAITING and steps[1]["approval_expires_at"]
    # 没人批就什么都没做：反证是把 awaiting 那支改成直接执行，这句立刻红
    assert assets.exists_name(ag_scene["project_id"], "character", "批了才建的角色") is False


def test_批了才执行且验收看真实业务行(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "燧人·批后才建"}, 1)
    agents.tick()
    assert c.post(f"/api/agent-steps/{step['id']}/review-sign",
                  json={"anchor": "execute", "decision": "approved",
                        "note": "参数对"}).status_code == 200
    agents.tick()
    after = agent_runs.step(step["id"])
    assert after["status"] == STEP_SUCCEEDED, after["error_message"]
    result = json.loads(after["result_json"])
    assert result["verified"] is True
    row = assets.get(result["asset_id"])
    assert row is not None and row["name"] == "燧人·批后才建"


def test_参数被改过旧批准不算数(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "批之前改名"}, 1)
    agents.tick()
    c.post(f"/api/agent-steps/{step['id']}/review-sign",
           json={"anchor": "execute", "decision": "approved", "note": ""})
    changed = agent_tools.validate("asset_register",
                                   {"kind": "character", "name": "批之后改掉的名字"})
    with get_conn() as conn:
        conn.execute("UPDATE agent_step SET args_json = ?, args_hash = ? WHERE id = ?",
                     (json.dumps(changed, ensure_ascii=False),
                      agent_tools.args_hash("asset_register", changed), step["id"]))
    agents.tick()
    assert agent_runs.step(step["id"])["status"] == STEP_AWAITING
    assert assets.exists_name(ag_scene["project_id"], "character",
                              "批之后改掉的名字") is False


def test_驳回让这一步失败且不执行(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "会被驳回的角色"}, 1)
    agents.tick()
    c.post(f"/api/agent-steps/{step['id']}/review-sign",
           json={"anchor": "execute", "decision": "rejected", "note": "名字不对"})
    agents.tick()
    after = agent_runs.step(step["id"])
    assert after["status"] == STEP_FAILED and after["error_code"] == "approval_rejected"
    assert assets.exists_name(ag_scene["project_id"], "character",
                              "会被驳回的角色") is False


def test_审批过期收成过期而不是永远挂着(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "过期这个角色"}, 1)
    agents.tick()
    assert agent_runs.step(step["id"])["status"] == STEP_AWAITING
    past = (datetime.now() - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE agent_step SET approval_expires_at = ? WHERE id = ?",
                     (past, step["id"]))
    agents.tick()
    after = agent_runs.step(step["id"])
    assert after["status"] == "expired" and after["error_code"] == "approval_expired"
    assert step["id"] not in [s["id"] for s in
                              agent_runs.awaiting_steps([ag_scene["project_id"]])]


def test_取消运行时待审步骤一起作废(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "取消这个角色"}, 1)
    agents.tick()
    assert c.post(f"/api/agent/runs/{run['id']}/cancel",
                  params={"reason": "不做了"}).status_code == 200
    assert agent_runs.step(step["id"])["status"] == "cancelled"
    assert c.get(f"/api/agent/runs/{run['id']}").json()["status"] == "cancelled"
    # 取消不能恢复：待审步骤已经作废，恢复了也没有可执行的参数
    assert c.post(f"/api/agent/runs/{run['id']}/resume").status_code == 409


def test_暂停时不往前推但到期审批仍然收掉(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "暂停这个角色"}, 1)
    agents.tick()                                   # 先让它停在等人批
    assert agent_runs.step(step["id"])["status"] == STEP_AWAITING
    c.post(f"/api/agent/runs/{run['id']}/pause")
    c.post(f"/api/agent-steps/{step['id']}/review-sign",
           json={"anchor": "execute", "decision": "approved", "note": "批了但暂停中"})
    agents.tick()
    # 暂停中不执行：批准已经在了，但这一步必须还不动
    assert agent_runs.step(step["id"])["status"] == STEP_AWAITING \
        or agent_runs.step(step["id"])["status"] == "expired"
    past = (datetime.now() - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("UPDATE agent_step SET approval_expires_at = ? WHERE id = ?",
                     (past, step["id"]))
    agents.tick()
    assert agent_runs.step(step["id"])["status"] == "expired"


def test_运行状态一律现算人的意愿优先(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    _add_step(run["id"], "episode_preflight", {"episode_id": ag_scene["episode_id"]}, 1)
    rows = agent_runs.steps(run["id"])
    assert agents.run_status(agent_runs.get(run["id"]), rows) == "running"
    agent_runs.set_will(run["id"], paused=True)
    assert agents.run_status(agent_runs.get(run["id"]), rows) == "paused"
    agent_runs.set_will(run["id"], cancelled=True, reason="人按的")
    # 取消排在一切步骤状态之前：哪怕下面全是 running，对外也只能说已取消
    assert agents.run_status(agent_runs.get(run["id"]), rows) == "cancelled"


def test_步骤停在执行中却没有作业指针按失败处理(as_role, clean_queue,
                                               plan_server, ag_scene):
    """崩在"写指针之前"的那一步不能永远挂着，也不能被猜成成功。

    猜成功的代价是重复执行一次付费动作，所以这里唯一的诚实答案是失败。
    """
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "text_job_submit",
                     {"capability": "text", "prompt": "写一段人物履历"}, 1)
    with get_conn() as conn:
        conn.execute("UPDATE agent_step SET status = 'running', job_id = NULL WHERE id = ?",
                     (step["id"],))
    agents.tick()
    after = agent_runs.step(step["id"])
    assert after["status"] == STEP_FAILED and after["error_code"] == "lost"


# ==================== 签字入口的三条边界 ====================
def test_锚点不认得就拒(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "锚点这个角色"}, 1)
    agents.tick()
    resp = c.post(f"/api/agent-steps/{step['id']}/review-sign",
                  json={"anchor": "artifact:0", "decision": "approved", "note": ""})
    assert resp.status_code == 422 and "execute" in resp.json()["detail"]


def test_不在等人时签会撞409(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "episode_preflight",
                     {"episode_id": ag_scene["episode_id"]}, 1)
    agents.tick()                        # 只读步骤直接做完，没人可签
    assert agent_runs.step(step["id"])["status"] == STEP_SUCCEEDED
    resp = c.post(f"/api/agent-steps/{step['id']}/review-sign",
                  json={"anchor": "execute", "decision": "approved", "note": ""})
    assert resp.status_code == 409


def test_签字写进的是审核中心那张表而不是新表(as_role, clean_queue,
                                             plan_server, ag_scene):
    """不变量 3：Review 只有一个写入口。`agent_approval` 那张表**没有建**，
    批准落在 `review_item` 的第三档上——这条就是那个决定的落库证据。
    """
    c = as_role("admin")
    run = _start(c, ag_scene)
    step = _add_step(run["id"], "asset_register",
                     {"kind": "character", "name": "落在审核表的角色"}, 1)
    agents.tick()
    c.post(f"/api/agent-steps/{step['id']}/review-sign",
           json={"anchor": "execute", "decision": "approved", "note": "落库证据"})
    with get_conn() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='agent_approval'").fetchone() is None
        row = conn.execute("SELECT * FROM review_item WHERE target_kind = 'agent_action' "
                           "AND target_id = ?", (step["id"],)).fetchone()
    assert row is not None and row["decision"] == "approved"
    assert row["base_version"] == step["args_hash"]      # 绑的是参数指纹


# ==================== 视图与口径 ====================
def test_概览四个数带口径同屏(as_role, clean_queue, plan_server, ag_scene):
    c = as_role("admin")
    _start(c, ag_scene)
    data = c.get("/api/agent/overview").json()
    for key in ("runs", "active", "awaiting_approval",
                "usable_configs", "total_configs"):
        assert key in data["counts"], key
    assert "待审批" in data["caliber"] or "步骤" in data["caliber"]
    assert data["tools"], "工具白名单必须念得出来，否则边界只能靠读代码"


def test_配置卡不泄露密钥值只给变量名(as_role, plan_server):
    cards = c_admin_configs(as_role("admin"))
    cloud = next(x for x in cards if x["backend"] == "cloud")
    assert cloud["key_env"] == "DEEPSEEK_API_KEY"
    assert isinstance(cloud["secret_present"], bool)
    body = json.dumps(cards, ensure_ascii=False)
    assert SECRET not in body            # 值可以存在，但绝不出现在响应里


def c_admin_configs(client):
    resp = client.get("/api/agent/configs")
    assert resp.status_code == 200
    return resp.json()["cards"]


def test_引擎没登记实例时探测回unknown不抛(as_role):
    """2026-11-06 接 Ollama 时撞到的真缺陷：`probe()` 在"没有启用实例"那一支
    漏了第三个参数，直接 TypeError。"还没配"恰恰是最常遇到的一档。
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM engine_instance WHERE engine_code = 'ollama'")
    status = engines.probe("ollama", "")
    assert status.state == "unknown" and "还没有登记" in status.detail
    assert status.values == {}


def test_本地文本腿是服务型但不是单模型型():
    """Ollama 属于"打一次本机服务"这一族（健康靠端点探测），
    但不属于"同一时刻只有一个当前模型"那一族——提交闸门不该拿它核对
    引擎当前选的是哪一档。有人把两个集合合并，这条会红。
    """
    assert "ollama" in engines.SERVICE_ENGINES
    assert "ollama" not in engines.MODEL_GATED_ENGINES
    assert "draw_things" in engines.MODEL_GATED_ENGINES


def test_智能体不越过发起人权限(as_role, plan_server, ag_scene, people):
    """工具跑在 worker 里，没有"当前请求人"，所以每一步都以发起人身份过判定。

    这里直接落一条"发起人是只读角色"的运行（绕过 HTTP），
    让执行器去登记资产：必须拒，且原话说的是发起人缺权限。
    """
    viewer = people["viewer"]
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO agent_run (project_id, goal, scene_key, plan_json,
                                      approval_valid_seconds, created_by)
               VALUES (?, ?, '', '[]', 3600, ?)""",
            (ag_scene["project_id"], GOAL, viewer["id"]))
        run_id = cursor.lastrowid
    step = _add_step(run_id, "asset_register",
                     {"kind": "character", "name": "越权登记的角色"}, 1)
    agents._execute(agent_runs.get(run_id), step)
    after = agent_runs.step(step["id"])
    assert after["status"] == STEP_FAILED and after["error_code"] == "forbidden"
    assert "权限" in after["error_message"]
    assert assets.exists_name(ag_scene["project_id"], "character",
                              "越权登记的角色") is False


def test_事件的每一档都有中文名(as_role):
    """23 档 `event_type` 的中文名只许有一份，而且必须与清单同长。

    原来它只住在界面（`views/agents.js` 的 `EVENT_LABEL`），服务端不下发，
    两份之间没有任何守卫：清单加一档，界面会安静地少说一句"发生了什么"，
    而那一句恰好是事件时间线唯一的内容（《系统设计说明书》§16 第 27 条）。
    现在中文名在 `agents.EVENT_LABEL`，随列表与详情下发；这条用例钉的就是
    "两把字典的键集合必须相等"——少一档红，多写一档不存在的也红。
    """
    import agents
    from repositories.agents import EVENT_TYPES

    assert set(agents.EVENT_LABEL) == set(EVENT_TYPES), (
        "事件清单与中文名对不上：缺 "
        + str(sorted(set(EVENT_TYPES) - set(agents.EVENT_LABEL)))
        + "／多 " + str(sorted(set(agents.EVENT_LABEL) - set(EVENT_TYPES))))
    # 空白标签等于"少说一档"，与缺键一样严重
    assert all(str(v).strip() for v in agents.EVENT_LABEL.values())
    # 23 这个数不写死：它由 EVENT_TYPES 决定，写死只是给下一次加档添一条要改的地方
    assert len(agents.EVENT_LABEL) == len(EVENT_TYPES) == 23


def test_事件中文名随响应下发(as_role):
    """界面不再自己抄一份——服务端不给，它就只能显示事件码。

    这一条要走 HTTP：只在服务层加了字典、忘了在响应里带出去，
    const 就在那儿摆着而界面永远拿不到（这是本仓库踩过的那类"加了字段没改 schema"）。
    """
    import agents

    c = as_role("admin")
    listed = c.get("/api/agent/runs", params={"project_id": 1})
    assert listed.status_code == 200, listed.text
    labels = listed.json()["event_labels"]
    assert labels == agents.EVENT_LABEL, "列表没带上事件中文名"
    # 详情也带（时间线在详情里画）
    runs = listed.json()["items"]
    if runs:
        detail = c.get(f"/api/agent/runs/{runs[0]['id']}").json()
        assert detail["event_labels"] == agents.EVENT_LABEL


def test_审批有效期四档都说得出秒数与标签(as_role):
    """接口给的是**秒数 + 标签**两个字段，不是二选一。

    原来 `default_valid_seconds` 回的是标签字符串（`dict(VALID_CHOICES).get(86400)`
    取到 "24 小时"），而字段名说 `_seconds`——界面拿它去比 `<option value>`，
    永远对不上，于是默认档静默落到第一项。这正是 §16 第 28 条登记的那处
    "界面上的承诺比代码少一步"。
    """
    from routers.agents import DEFAULT_VALID_SECONDS, VALID_CHOICES

    c = as_role("admin")
    body = c.get("/api/agent/plan-options",
                 params={"project_id": 1}).json()
    choices = body["approval_choices"]
    # 四档一档不少，且每档两个字段都在
    assert len(choices) == len(VALID_CHOICES) == 4
    assert all(isinstance(x["seconds"], int) and x["label"] for x in choices)
    assert [(x["seconds"], x["label"]) for x in choices] == list(VALID_CHOICES)
    # 默认那一档：类型必须是 int（界面要拿它比 value），值必须是四档之一
    assert isinstance(body["default_valid_seconds"], int), (
        "default_valid_seconds 必须是秒数，不是标签：" + repr(body["default_valid_seconds"]))
    assert body["default_valid_seconds"] == DEFAULT_VALID_SECONDS
    assert body["default_valid_seconds"] in [x["seconds"] for x in choices]
    # 标签另给一格，别让界面自己拼
    assert body["default_valid_label"] == dict(VALID_CHOICES)[DEFAULT_VALID_SECONDS]


def test_界面不再自己抄事件中文名与有效期档位():
    """守住"第二份定义"：这两样谁也不许在 js 里再写一遍。

    读源码而不是走浏览器：这两个字典在 DOM 上看不出来，
    而它们恰恰是"清单改了界面少说一档"的地方。
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "views"
          / "agents.js").read_text()
    # 事件中文名：只许留函数与说明，字典本体必须已经删掉
    assert "const EVENT_LABEL = {" not in js, "界面又抄了一份事件中文名"
    assert "eventLabel(" in js, "界面应当按服务端下发的表念"
    # 有效期：不许再写死"默认是哪一档"，也不许自己造一条档位。
    # 只查代码行（去掉注释与空行）：注释里复述旧写法是说明，不是第二份定义。
    code = "\n".join(line for line in js.split("\n")
                      if not line.strip().startswith(("//", "*", "/*")))
    assert "=== 86400" not in code, "默认档还写死在前端"
    assert "seconds: 86400" not in code, "档位还写死在前端"


def test_签字回执要显示出来(as_role):
    """服务端回的 note（"批准已记下，下一拍执行"）必须被界面用上。

    原来它一个字都没显示：`util.run()` 只在失败时说话，成功时静默刷新，
    于是人签完只看到列表变了，不知道成没成、接下来等什么。
    这条读源码——那个 note 在 DOM 上不一定抓得到（取决于当时有没有待签项），
    而"界面有没有接住这个字段"是源码层面就能钉死的事。
    """
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "frontend" / "js" / "views"
          / "review-actions.js").read_text()
    # 必须真的接收 signAgentStep 的返回值并把它交出去（现在落在模块变量 `flash`）
    assert "out.note" in js, "签字回执没被接住"
    assert "flash = { tone: 'ok'" in js, "成功回执没有落到模块状态里"
    # 回执必须画在**这一栏顶部**而不是详情卡里：签完那一步就离开待批队列，
    # 详情卡整块消失——写进卡片等于"签成了，但你看不到它说的话"。
    assert "bar ${flash.tone}" in js, "成功回执要按全站的成功样式显示"
    # 而且它必须出现在 renderActions 的模板里（不在 detailHtml 里）
    head, _, tail = js.partition("function detailHtml")
    assert "${flashBar}" in head, "回执要画在动作审批那一栏，不是某一张卡片里"
    # 后端的 note 里确实有"下一拍"这句人话（别把它删成空串）
    from pathlib import Path as _P

    backend = (_P(__file__).resolve().parents[1] / "routers" / "review.py").read_text()
    assert "worker 的下一拍" in backend


def test_付费步骤批准之后才带得动云端确认(as_role, clean_queue, plan_server, ag_scene, monkeypatch):
    """`text_job_submit` 是 risk=paid：那一步的"云端计费确认"从哪来。

    原来 `agent_tools` 恒传 `user_confirmed_cloud=False`，于是**默认轨一旦指向云端，
    这类步骤注定失败**——失败原因是"没有人能替人做那次确认"，这不算保护，
    是让这条路永远走不通（《系统设计说明书》§16 第 25 条）。

    现在确认由审核中心那条批准提供：它落库、绑参数指纹。所以这里要钉两件事：
    ① 签字之前这一步不执行；② 签字之后才带着确认去提交。
    用 monkeypatch 记账而不是真跑一条云端作业——本条测的是"确认传没传到"
    （真调用那一条腿由 mock／假适配器那条用例管）。
    """
    import agents
    import services

    plan_server.reply = json.dumps([
        {"tool": "text_job_submit",
         "args": {"capability": "text", "prompt": "这一条会被真的提交吗"},
         "why": "写一段说明"}
    ])
    seen = []
    # **只在步骤执行期**换成记账版：算计划那一枪也走 `submit_job`，
    # 一上来就换掉会让"发起运行"这一步自己先失败（第一版就这么写的）。
    real_submit = services.submit_job
    armed = {"on": False}

    def spy(payload, created_by):
        if not armed["on"]:
            return real_submit(payload, created_by)
        seen.append({"confirmed": bool(payload.user_confirmed_cloud)})
        raise services.JobInvalid("测试里不真排作业：只记这一次调用的确认位")

    monkeypatch.setattr(services, "submit_job", spy)
    c = as_role("admin")
    run = _start(c, ag_scene)
    # 算计划那条作业得真跑完（`agents.tick()` 只认终态为成功的计划作业），
    # 与 `test_计划回来落成步骤` 同一条路。
    assert worker.run_once("w-paid") == run["plan_job_id"]
    agents.tick()
    step = next((s for s in agent_runs.steps(run["id"])), None)
    assert step is not None, "计划没有落成步骤"
    assert step["status"] == "awaiting_approval", f"付费步骤没有停下来等人批：{step['status']}"
    armed["on"] = True      # 从这里起，任何一次提交都记下来（不该发生）
    assert step["risk"] == "paid"
    # ① 还没签：一步都不许执行，提交一次都不许发生
    for _ in range(3):
        agents.tick()
    assert seen == [], f"没签字就提交了作业：{seen}"

    # 审批卡要把"这一步会花钱"说出来——授权得让人看得见，不能靠猜
    card = c.get(f"/api/agent-steps/{step['id']}/review").json()
    assert card["paid"] is True
    assert "计费" in card["cloud_confirm_note"]

    # ② 签字之后：带着确认去提交，那一枪才发得出去
    signed = c.post(f"/api/agent-steps/{step['id']}/review-sign",
                    json={"anchor": "execute", "decision": "approved",
                          "note": "同意花这一次的钱"})
    assert signed.status_code == 200, signed.text
    # 推进到执行那一步：提交被我们的 spy 拦下，但它已经过去了，确认位必须为真
    for _ in range(4):
        agents.tick()
    assert seen, "签了字却一次都没走到提交"
    assert seen[0]["confirmed"] is True, (
        "签过字的付费步骤仍然没有带云端确认——路上又变回 False 了")


def test_只读步骤不带云端确认(as_role, clean_queue, plan_server, ag_scene, monkeypatch):
    """反向半边：没有签字的那一类步骤，`cloud_confirmed` 必须是假。

    只读步骤从 `pending` 直接进场（不经过审批）。若它也带着确认，
    那么"确认"这件事就退化成了一个恒真的字段，闸门形同虚设。
    """
    import agents

    plan_server.reply = _fix_episode(PLAN_READ, ag_scene["episode_id"])
    c = as_role("admin")
    run = _start(c, ag_scene)
    assert worker.run_once("w-read") == run["plan_job_id"]
    captured = {}

    real = agent_tools.execute

    def spy(tool_code, args, ctx):
        captured.update(ctx)
        return real(tool_code, args, ctx)

    agent_tools.execute = spy
    try:
        for _ in range(6):
            agents.tick()
    finally:
        agent_tools.execute = real
    assert captured, "只读步骤没有执行"
    assert captured.get("cloud_confirmed") is False, (
        "没签过字的只读步骤也带了云端确认：闸门成了摆设")
