"""产线编排（0026／阶段 B／裁定 D3）。

这一份测试盯的是附件 §12.1／§12.2 那两张清单：模板合法性、版本冻结、节点展开、
依赖解析、成环拒绝、状态流转、调度、幂等、重试、暂停、恢复、取消，以及产线与
Job 的十种耦合情形。

两条口径先说清，免得读断言时误会：

* **作业用假适配器真跑**（`worker.run_once`），不往库里塞假状态。产物是真文件，
  所以"产物被清理后节点该失败"这条测得到。
* **默认轨临时指向 mock**。真路线在这台机器上跑不动（没有权重），
  而产线派发作业时不传 provider／模型（走平台默认轨），所以必须把默认轨换成
  假适配器，才能验"产线真的把活交给了那一个队列"。`provider_state` 负责还原。
"""
import json
import uuid

import pytest

import adapters
import pipeline
import precheck
import services
import worker
from config import JOBS_DIR
from db import get_conn
from repositories.content import episodes, shots
from repositories.jobs import jobs as job_repo
from repositories.pipeline import pipeline_repo
from repositories.projects import projects as project_repo
from repositories.reviews import reviews

# 合成节点那两条要真跑 FFmpeg 造产物。守卫**只能挂在那两条上，不能写成本文件的
# pytestmark**——这一份有 30 多条测试，绝大多数与 ffmpeg 无关，
# 模块级跳过等于在没有 ffmpeg 的机器上把整条产线回归悄悄关掉。
# `_has_ffmpeg` 复用 test_composition 里那一个定义，不在这里再数一遍 PATH。
from test_composition import _has_ffmpeg

FAST = {"sleep": 0.01}          # 假适配器的睡眠秒数：够走完分支，不拖慢测试


def _mock_adapter(capability: str, backend: str):
    """给 mock 造一个"登记过"的适配器外壳。

    `adapters.find` 找不到 mock（它故意不进清单），而 `resolve_provider`
    要求"没有登记适配器就不给走"。测试要让默认轨指向 mock，就得补这一层壳；
    参数白名单直接用 `adapters.MOCK_PARAMS`，与 worker 那一边校验的是同一份。
    """
    return adapters.Adapter(
        code="mock", label="假适配器（测试壳）", capability=capability,
        backend=backend, source="tests/test_pipeline.py",
        params_source="adapters.MOCK_PARAMS",
        models=(adapters.ModelSpec(code=adapters.MOCK_MODEL, label="mock-1",
                                   params=adapters.MOCK_PARAMS),))


@pytest.fixture
def mock_route(monkeypatch, provider_state):
    """把图像与视频的默认轨换成假适配器，函数结束自动还原。"""
    real_find = adapters.find

    def _find(capability, backend, provider):
        if (provider or "").strip().lower() == "mock":
            return _mock_adapter(capability, backend)
        return real_find(capability, backend, provider)

    monkeypatch.setattr(adapters, "find", _find)
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
    return _find


def _template(definition: dict, name: str | None = None) -> int:
    """落一份测试用模板，返回 id。名字带 uuid：模板表是全局的，别互相踩。"""
    row = pipeline_repo.ensure_template(
        name=name or f"测试产线_{uuid.uuid4().hex[:6]}", version=1,
        description="测试用", definition=definition, builtin=False)
    return row["id"]


# 一份"能一路跑完"的流程：内部检查 → 人工确认 → 逐镜关键帧 → 逐镜验收 →
# 逐镜视频 → 逐镜产物检查 → 成片准备（这一格永远是 skipped，阶段 D 才做）
FULL = {"nodes": [
    {"key": "read_project", "type": "project_input", "depends_on": []},
    {"key": "read_episode", "type": "episode_input", "depends_on": ["read_project"]},
    {"key": "check_shots", "type": "shot_validate", "depends_on": ["read_episode"]},
    {"key": "gate", "type": "human_gate", "depends_on": ["check_shots"]},
    {"key": "keyframe", "type": "image_generate", "depends_on": ["gate"],
     "fan_out": "shot", "params": FAST},
    {"key": "review_keyframe", "type": "image_review", "depends_on": ["keyframe"],
     "fan_out": "shot"},
    {"key": "shot_video", "type": "video_generate", "depends_on": ["review_keyframe"],
     "fan_out": "shot", "params": FAST},
    {"key": "check_video", "type": "artifact_validate", "depends_on": ["shot_video"],
     "fan_out": "shot"},
    {"key": "prepare_export", "type": "export_prepare", "depends_on": ["check_video"]},
]}


def _project(people, n_shots: int = 2, with_doc: bool = True,
             prompts: bool = True) -> dict:
    """建一套真实输入：项目 → 集 → 镜头 → 剧本正文。"""
    pid = project_repo.create(f"产线_{uuid.uuid4().hex[:6]}", "", [],
                              people["editor"]["id"])["id"]
    eid = episodes.create(pid, "第 1 集")["id"]
    sids = [shots.create(eid, f"镜 {i + 1}",
                         f"提示词 {i + 1}" if prompts else "", 5.0, [])["id"]
            for i in range(n_shots)]
    doc = None
    if with_doc:
        doc = services.create_document(pid, eid, "script", "第一集剧本",
                                       "燧人蹲在洞口。\n", "测试账号")["row"]
    return {"project_id": pid, "episode_id": eid, "shot_ids": sids,
            "document": doc}


def _pump(run_id: int, rounds: int = 14):
    """模拟 worker 的节拍：推进产线 → 把队列跑空 → 再推进，直到没有新作业。

    这就是 `worker.main()` 循环里那两行的测试版；区别只是这里显式点名一条运行，
    免得把别的用例留下的运行也带着走。
    """
    for _ in range(rounds):
        pipeline.advance_run(run_id)
        ran = 0
        while worker.run_once("w-pipeline") is not None:
            ran += 1
        if ran == 0:
            break
    return pipeline.run_view(run_id)


def _nodes(view: dict) -> dict:
    return {n["node_key"]: n for n in view["nodes"]}


def _sign_doc(setup, people, decision="approved"):
    """给剧本当前那一版签一个字——产线的 human_gate 等的就是它。"""
    doc = setup["document"]
    version = services.read_content(doc)["version"]
    return reviews.upsert_sign(
        project_id=setup["project_id"], target_kind=reviews.DOCUMENT,
        target_id=doc["id"], gate_id=None, base_version=version,
        anchor="document", decision=decision, note="测试签字",
        user_id=people["editor"]["id"])


def _sign_job(job_id: int, setup, people, decision="approved", anchor="artifact"):
    """给一条作业的产物签字——image_review／shot_review 等的是它。"""
    job = job_repo.get(job_id)
    return reviews.upsert_sign(
        project_id=setup["project_id"], target_kind=reviews.JOB,
        target_id=job_id, gate_id=None,
        base_version=services.artifact_version(job),
        anchor=anchor, decision=decision, note="测试签字",
        user_id=people["editor"]["id"])


# ---------- 模板 ----------
def test_模板合法性一次说清所有问题():
    errors = pipeline.validate_definition({"nodes": [
        {"key": "a", "type": "project_input", "depends_on": ["a"]},        # 自依赖
        {"key": "a", "type": "episode_input", "depends_on": []},           # key 重复
        {"key": "b", "type": "不存在的类型", "depends_on": ["没有这个"]},   # 类型与依赖都不认识
        {"key": "c", "type": "image_generate", "depends_on": [],
         "fan_out": "episode"},                                           # fan_out 只支持 shot
        {"key": "d", "type": "project_input", "depends_on": [],
         "fan_out": "shot"},                                              # 这一类不能按镜头展开
    ]})
    joined = "；".join(errors)
    for needle in ("依赖自己", "key 重复", "类型不认识", "不存在的节点",
                   "fan_out 只支持 shot", "不能按镜头展开"):
        assert needle in joined, joined
    # 合法的定义就是空清单；内置三份都必须过这一关
    assert pipeline.validate_definition(FULL) == []
    for _, _, _, definition in pipeline.BUILTIN_TEMPLATES:
        assert pipeline.validate_definition(definition) == []


def test_成环被拒():
    errors = pipeline.validate_definition({"nodes": [
        {"key": "a", "type": "project_input", "depends_on": ["c"]},
        {"key": "b", "type": "episode_input", "depends_on": ["a"]},
        {"key": "c", "type": "shot_validate", "depends_on": ["b"]},
    ]})
    assert any("依赖成环" in e for e in errors), errors
    assert "a" in errors[-1] and "c" in errors[-1]


def test_内置模板补齐后重复启动不改历史(monkeypatch):
    """`ensure_builtin_templates()` 是 INSERT OR IGNORE：改代码里的定义也不会
    动已经发布的那一版——要改流程必须 version+1，正在跑的运行认它创建时那一版。"""
    first = pipeline.ensure_builtin_templates()
    rows = pipeline_repo.list_templates(enabled_only=False)
    builtin = [r for r in rows if r["builtin"]]
    assert len(builtin) == len(pipeline.BUILTIN_TEMPLATES)
    target = next(r for r in builtin if r["name"] == "标准漫剧单集生产")
    with get_conn() as conn:
        conn.execute("UPDATE pipeline_template SET definition_json = ? WHERE id = ?",
                     (json.dumps({"nodes": [{"key": "x", "type": "project_input",
                                            "depends_on": []}]}), target["id"]))
    assert pipeline.ensure_builtin_templates() == 0        # 一条都没新插
    again = pipeline_repo.get_template(target["id"])
    assert json.loads(again["definition_json"])["nodes"][0]["key"] == "x"
    assert first >= 0


# ---------- 建运行与展开 ----------
def test_建运行会冻结模板版本并展开节点(mock_route, people):
    setup = _project(people, n_shots=2)
    tid = _template(FULL)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                               people["editor"]["id"])
    assert view["template_version"] == 1
    nodes = _nodes(view)
    # 5 个不按镜头展开 ＋ 4 类按镜头展开 × 2 镜 ＝ 13
    assert len(nodes) == 5 + 4 * 2
    # 展开出来的 key 带着镜头 id，一镜一条支线
    assert f"keyframe#s{setup['shot_ids'][0]}" in nodes
    assert nodes[f"keyframe#s{setup['shot_ids'][0]}"]["depends_on"] == ["gate"]
    # 汇聚节点依赖**全部**镜头实例：镜头 2 失败会挡住它，但不会让镜头 1 重跑
    assert sorted(nodes["prepare_export"]["depends_on"]) == sorted(
        [f"check_video#s{s}" for s in setup["shot_ids"]])
    # 输入快照冻结在建运行那一刻：剧本版本与镜头四要素都答得出来
    assert view["document"]["base_version"]
    assert view["shots"] == 2
    # 建完立刻推进了一轮：前三个内部节点当场做完，人工确认停在等签字
    assert nodes["read_project"]["status"] == "completed"
    assert nodes["check_shots"]["status"] == "completed"
    assert nodes["gate"]["status"] == "waiting_review"
    assert nodes["keyframe#s%d" % setup["shot_ids"][0]]["status"] == "blocked"
    assert view["status"] == "waiting_review"


def test_没有镜头时不建运行_没有正文时人工确认那格失败(mock_route, people):
    pid = project_repo.create(f"空_{uuid.uuid4().hex[:6]}", "", [],
                              people["editor"]["id"])["id"]
    eid = episodes.create(pid, "第 1 集")["id"]
    tid = _template(FULL)
    with pytest.raises(pipeline.PipelineInvalid) as exc:
        pipeline.create_run(pid, eid, tid, people["editor"]["id"])
    assert "还没有镜头" in str(exc.value)

    # 没有正文不拦建运行（展开节点用不着它），但人工确认那一格必须明确失败：
    # 不能说"没有稿子要确认，那就当确认过了"。
    setup = _project(people, n_shots=1, with_doc=False)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    gate = _nodes(view)["gate"]
    assert gate["status"] == "failed"
    assert gate["error_code"] == "document_missing"
    assert view["status"] == "failed"

    # 集不属于该项目：不是"看不见"，是请求本身不成立
    other = _project(people, n_shots=1)
    with pytest.raises(pipeline.PipelineInvalid):
        pipeline.create_run(other["project_id"], setup["episode_id"],
                            _template(FULL), people["editor"]["id"])


def test_同一集只允许一条未结束的运行_幂等键复用(mock_route, people):
    setup = _project(people, n_shots=1)
    tid = _template(FULL)
    first = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                                people["editor"]["id"], "同一个键")
    # 同一个幂等键重放：拿回同一条运行，不建第二条
    again = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                                people["editor"]["id"], "同一个键")
    assert again["id"] == first["id"]
    # 不带键再点一次"开始生产"：被业务闸挡住，不排两套镜头作业
    with pytest.raises(pipeline.PipelineConflict) as exc:
        pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                            people["editor"]["id"])
    assert "还没结束" in str(exc.value)
    # 同一个键被别的项目占用：409，不 silently 建一条新的
    other = _project(people, n_shots=1)
    with pytest.raises(pipeline.PipelineConflict):
        pipeline.create_run(other["project_id"], other["episode_id"], tid,
                            people["editor"]["id"], "同一个键")


def test_云端节点不代发(mock_route, people):
    """云端作业必须逐条确认预计费用；产线替人按下去＝在人没看价格时花钱。"""
    setup = _project(people, n_shots=1)
    tid = _template({"nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "cloud_video", "type": "video_generate",
         "depends_on": ["read_project"], "execution_mode": "cloud"},
    ]})
    with pytest.raises(pipeline.PipelineInvalid) as exc:
        pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                            people["editor"]["id"])
    assert "不代发云端作业" in str(exc.value) and "cloud_video" in str(exc.value)


def test_模板改版不影响已经在跑的运行(mock_route, people):
    setup = _project(people, n_shots=1)
    tid = _template(FULL, name=f"冻结_{uuid.uuid4().hex[:6]}")
    view = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                               people["editor"]["id"])
    before = len(view["nodes"])
    # 同名 version+1 再加一份，节点数故意不一样
    pipeline_repo.ensure_template(
        name=pipeline_repo.get_template(tid)["name"], version=2, description="改版",
        definition={"nodes": [{"key": "only", "type": "project_input",
                               "depends_on": []}]}, builtin=False)
    after = pipeline.run_view(view["id"])
    assert after["template_version"] == 1 and len(after["nodes"]) == before


# ---------- 调度与状态流转 ----------
def test_签字之后一路跑到成片准备(mock_route, people, clean_queue):
    """阶段 B 的主干：人工确认 → 关键帧 → 验收 → 视频 → 产物检查 → 导出准备。

    每一步都由"审核中心的签字"或"Job 的真实状态"推动，产线自己不写结论。
    """
    setup = _project(people, n_shots=2)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    _sign_doc(setup, people)
    view = _pump(run_id)
    nodes = _nodes(view)
    assert nodes["gate"]["status"] == "completed"
    # 两镜的关键帧都成了产物审核，等签字
    for sid in setup["shot_ids"]:
        assert nodes[f"keyframe#s{sid}"]["status"] == "completed"
        assert nodes[f"keyframe#s{sid}"]["job_id"]
        assert nodes[f"review_keyframe#s{sid}"]["status"] == "waiting_review"
        assert nodes[f"shot_video#s{sid}"]["status"] == "blocked"
    assert view["status"] == "waiting_review"

    for sid in setup["shot_ids"]:
        _sign_job(nodes[f"keyframe#s{sid}"]["job_id"], setup, people)
    view = _pump(run_id)
    nodes = _nodes(view)
    for sid in setup["shot_ids"]:
        assert nodes[f"shot_video#s{sid}"]["status"] == "completed"
        assert nodes[f"check_video#s{sid}"]["status"] == "completed"
    # 成片准备是阶段 D：明确 skipped，不假装合成了成片
    assert nodes["prepare_export"]["status"] == "skipped"
    assert nodes["prepare_export"]["output"]["reason"]
    assert view["status"] == "completed_with_errors"       # 有 skipped 就不算干净完成
    assert view["progress"]["done"] == view["progress"]["total"]
    assert view["finished_at"]
    # 节点行里没有一列抄 Job 的事实：状态与产物都还去 Job 那边读
    job = job_repo.get(nodes[f"shot_video#s{setup['shot_ids'][0]}"]["job_id"])
    assert job["status"] == "succeeded"
    assert nodes[f"shot_video#s{setup['shot_ids'][0]}"]["job_status"] == "succeeded"


def test_重复推进不会重复派活(mock_route, people, clean_queue):
    """幂等三层：原子转态、确定性幂等键、Job 表的 UNIQUE。这里验前两层的落点。"""
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    _sign_doc(setup, people)
    pipeline.advance_run(view["id"])
    once = [n for n in pipeline.run_view(view["id"])["nodes"] if n["job_id"]]
    for _ in range(4):                       # 连点五次"推进"
        pipeline.advance_run(view["id"])
    again = [n for n in pipeline.run_view(view["id"])["nodes"] if n["job_id"]]
    assert [n["job_id"] for n in once] == [n["job_id"] for n in again]
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]
        keys = [r["idempotency_key"] for r in conn.execute(
            "SELECT idempotency_key FROM job WHERE idempotency_key LIKE 'pl-%'")]
    assert len(keys) == len(set(keys))
    assert total >= len(keys)
    # 键是确定性的：同一条运行、同一个节点、同一次重试，永远同一个键
    node = next(n for n in again if n["node_key"].startswith("keyframe"))
    assert f"pl-{view['id']}-{node['node_key']}-a1" in keys


def test_一镜失败只重试那一镜(mock_route, people, clean_queue):
    setup = _project(people, n_shots=2)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    _sign_doc(setup, people)
    # 让第二镜的关键帧失败：改的是它那条作业的参数，不是别人的
    pipeline.advance_run(run_id)
    nodes = _nodes(pipeline.run_view(run_id))
    good, bad = setup["shot_ids"]
    job_repo.get(nodes[f"keyframe#s{bad}"]["job_id"])
    with get_conn() as conn:
        conn.execute("UPDATE job SET params_json = ? WHERE id = ?",
                     (json.dumps({"sleep": 0.01, "fail": True}),
                      nodes[f"keyframe#s{bad}"]["job_id"]))
    view = _pump(run_id)
    nodes = _nodes(view)
    assert nodes[f"keyframe#s{bad}"]["status"] == "failed"
    assert nodes[f"keyframe#s{bad}"]["error_code"] == "mock_failed"
    assert nodes[f"keyframe#s{good}"]["status"] == "completed"
    assert nodes[f"review_keyframe#s{good}"]["status"] == "waiting_review"
    # 坏的这一镜，下游全挡住；好的那一镜照常往前走
    assert nodes[f"shot_video#s{bad}"]["status"] == "blocked"
    assert view["status"] == "failed"
    # 好那一镜还在等签字，所以这条运行**不该**收尾——收尾条件见
    # test_失败收尾之后同一集能再起一条
    assert view["finished_at"] == ""

    old_job = nodes[f"keyframe#s{bad}"]["job_id"]
    jobs_before = _count_jobs()
    retried = pipeline.retry_node(nodes[f"keyframe#s{bad}"]["id"],
                                  people["editor"]["id"])
    new_node = next(n for n in retried["nodes"]
                    if n["node_key"] == f"keyframe#s{bad}")
    assert new_node["retry_count"] == 1
    assert new_node["job_id"] != old_job
    # 旧作业不动：它是账，重试不是抹掉历史
    old = job_repo.get(old_job)
    assert old["status"] == "failed"
    assert old["superseded_by_job_id"] == new_node["job_id"]
    new_job = job_repo.get(new_node["job_id"])
    assert new_job["retry_of_job_id"] == old_job
    assert new_job["attempt"] == int(old["attempt"] or 1) + 1
    # **只多了一条**作业：节点跟着 retry 出来的那条走 running，
    # 不再回 pending 让调度器又提交一条（那一格就会有两条作业，其中一条没人认）
    assert _count_jobs() - jobs_before == 1
    assert new_job["status"] == "queued" and new_node["status"] == "running"

    # 原因没除掉，重试就是再失败一次：参数是原样带过来的，重试不会替人修好它
    view = _pump(run_id)
    still = next(n for n in view["nodes"] if n["node_key"] == f"keyframe#s{bad}")
    assert still["status"] == "failed" and still["retry_count"] == 1

    # 人查清楚之后改掉参数再重试，这一格才真的往前走（真实修法在生成中心或稿子里）
    with get_conn() as conn:
        conn.execute("UPDATE job SET params_json = ? WHERE id = ?",
                     (json.dumps({"sleep": 0.01}), still["job_id"]))
    pipeline.retry_node(still["id"], people["editor"]["id"])
    view = _pump(run_id)
    nodes = _nodes(view)
    assert nodes[f"keyframe#s{bad}"]["status"] == "completed"
    assert nodes[f"keyframe#s{bad}"]["retry_count"] == 2
    assert nodes[f"review_keyframe#s{bad}"]["status"] == "waiting_review"
    # 好的那一镜从头到尾没被重跑：它的作业还是原来那一条
    assert nodes[f"keyframe#s{good}"]["retry_count"] == 0


def test_失败收尾之后同一集能再起一条(mock_route, people, clean_queue):
    """阶段 C 验收撞出来的第二个口子：失败的下游永远停在"等待前置"，
    运行就永远凑不齐"全部终态"，`finished_at` 一直是空，于是同一集被自己挡住（409）。
    收尾条件改成"全部终态，或只剩跑不到的"之后，失败也需要收尾。
    """
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    _sign_doc(setup, people)
    pipeline.advance_run(run_id)
    sid = setup["shot_ids"][0]
    node = _nodes(pipeline.run_view(run_id))[f"keyframe#s{sid}"]
    with get_conn() as conn:
        conn.execute("UPDATE job SET params_json = ? WHERE id = ?",
                     (json.dumps({"sleep": 0.01, "fail": True}), node["job_id"]))
    view = _pump(run_id)
    assert view["status"] == "failed"
    assert view["finished_at"], "失败的运行也必须收尾"
    # 下游没跑过：仍停在"等待前置"，那是事实，不是错误
    assert _nodes(view)[f"shot_video#s{sid}"]["status"] == "blocked"

    # 重试会把已收尾的运行重新打开，节拍才肯继续往下派活
    with get_conn() as conn:
        conn.execute("UPDATE job SET params_json = ? WHERE id = ?",
                     (json.dumps({"sleep": 0.01}), node["job_id"]))
    retried = pipeline.retry_node(node["id"], people["editor"]["id"])
    assert retried["finished_at"] == ""
    view = _pump(run_id)
    nodes = _nodes(view)
    assert nodes[f"keyframe#s{sid}"]["status"] == "completed"
    assert nodes[f"review_keyframe#s{sid}"]["status"] == "waiting_review"

    # 收掉它，把这一集让出来
    pipeline.cancel_run(run_id, "验收收尾")
    again = pipeline.create_run(setup["project_id"], setup["episode_id"],
                                _template(FULL), people["editor"]["id"])
    assert again["id"] != run_id


def test_驳回的审核节点不给重试(mock_route, people, clean_queue):
    """驳回意味着产物要重做；重试"等签字"这一格没有意义。"""
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    _sign_doc(setup, people, decision="rejected")
    view = _pump(view["id"])
    gate = _nodes(view)["gate"]
    assert gate["status"] == "failed" and gate["error_code"] == "review_rejected"
    assert gate["review_id"]                    # 只引用签字行，不抄结论
    with pytest.raises(pipeline.PipelineConflict) as exc:
        pipeline.retry_node(gate["id"], people["editor"]["id"])
    assert "驳回" in str(exc.value)


def test_退回不改状态_改判才动(mock_route, people, clean_queue):
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    _sign_doc(setup, people, decision="returned")
    view = _pump(view["id"])
    assert _nodes(view)["gate"]["status"] == "waiting_review"   # 退回＝再改改，不是终局
    _sign_doc(setup, people, decision="approved")               # 同一锚点改判
    view = _pump(view["id"])
    assert _nodes(view)["gate"]["status"] == "completed"


def test_正文改了_旧签字不再算数(mock_route, people, clean_queue):
    """产线读签字时按**当前正文版本**读：改了稿子，旧签字自动失效，节点继续等。"""
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    _sign_doc(setup, people)
    services.save_content(setup["document"], "改过一次的正文。\n",
                          services.read_content(setup["document"])["version"],
                          "测试账号", people["editor"]["id"])
    view = _pump(view["id"])
    assert _nodes(view)["gate"]["status"] == "waiting_review"
    # 新版本再签一次就放行
    _sign_doc(setup, people)
    view = _pump(view["id"])
    assert _nodes(view)["gate"]["status"] == "completed"


def test_产物被清理_审核那一档立刻失配(mock_route, people, clean_queue):
    """产物审核的版本令牌是清单摘要：文件没了，旧签字就不再算数。

    产线这边**不回溯改判**：节点记的是"当时签过、当时产物在"，那是事实；
    失配体现在审核中心那一页（stale＝true、签字清空），人要重判就去那里重判。
    """
    setup = _project(people, n_shots=1)
    tid = _template({"nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "keyframe", "type": "image_generate", "depends_on": ["read_project"],
         "fan_out": "shot", "params": FAST},
        {"key": "check", "type": "artifact_validate", "depends_on": ["keyframe"],
         "fan_out": "shot"},
        {"key": "review", "type": "shot_review", "depends_on": ["check"],
         "fan_out": "shot"},
    ]})
    view = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                               people["editor"]["id"])
    sid = setup["shot_ids"][0]
    view = _pump(view["id"])
    nodes = _nodes(view)
    assert nodes[f"check#s{sid}"]["status"] == "completed"
    assert nodes[f"review#s{sid}"]["status"] == "waiting_review"
    keyframe_job = nodes[f"keyframe#s{sid}"]["job_id"]

    _sign_job(keyframe_job, setup, people)
    view = _pump(view["id"])
    nodes = _nodes(view)
    assert nodes[f"review#s{sid}"]["status"] == "completed"
    assert nodes[f"review#s{sid}"]["review_id"]      # 只引用签字行，不抄结论
    assert view["status"] == "completed"

    rel = json.loads(job_repo.get(keyframe_job)["result_paths_json"])[0]
    (JOBS_DIR / rel).unlink()
    state = services.job_review_state(job_repo.get(keyframe_job))
    assert state["stale"] is True and state["signatures"] == []
    assert state["artifacts"][0]["exists"] is False
    # 节点仍然是"已完成"：那是当时的事实，产物后来没了不由产线偷偷改判
    assert _nodes(pipeline.run_view(view["id"]))[f"review#s{sid}"]["status"] == "completed"
    # 但它在产物审核队列里带着失配标记，人能看见"这份签字对的是旧产物"
    queue = services.job_review_queue(setup["project_id"])
    mine = next(row for row in queue if row["job_id"] == keyframe_job)
    assert mine["stale"] is True and mine["signatures"] == []


def test_条件图从上游关键帧接过来(mock_route, people, clean_queue):
    """默认轨是 I2V 时，视频节点的条件图必须来自上游关键帧产物。

    模板里写不了路径（路径运行期才知道），所以由调度器按白名单缺什么补什么。
    """
    setup = _project(people, n_shots=1)
    # 把视频默认轨换成 mlx-video 的 I2V 档：它的白名单里 image 是必填
    _set_video_route("mlx-video", "wan2.2-i2v-a14b")
    tid = _template({"nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "keyframe", "type": "image_generate", "depends_on": ["read_project"],
         "fan_out": "shot", "params": FAST},
        {"key": "shot_video", "type": "video_generate", "depends_on": ["keyframe"],
         "fan_out": "shot", "params": {"image": "", "steps": 4}},
    ]})
    # 空字符串等于没给：白名单要的是路径。
    # 这里不能带 mock 的 sleep——参数是按**当前默认轨**的白名单校验的，
    # 换成 I2V 档之后 sleep 就不认识了（这正是要在界面上说清的一条限制）。
    view = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                               people["editor"]["id"])
    run_id = view["id"]
    sid = setup["shot_ids"][0]
    assert worker.run_once("w-pipeline") is not None       # 关键帧跑完
    view = pipeline.advance_run(run_id)                    # 这一轮才派视频节点
    node = _nodes(view)[f"shot_video#s{sid}"]
    assert node["job_id"], node["error_message"]
    artifact = json.loads(job_repo.get(
        _nodes(view)[f"keyframe#s{sid}"]["job_id"])["result_paths_json"])[0]
    got = json.loads(job_repo.get(node["job_id"])["params_json"])
    assert got["image"].endswith(artifact)
    assert got["steps"] == 4                     # 模板给的参数原样保留
    # 作业真的按 I2V 那一档建：路线仍由平台默认轨决定，产线没有替人改档
    job = job_repo.get(node["job_id"])
    assert (job["provider_code"], job["model_code"]) == ("mlx-video",
                                                         "wan2.2-i2v-a14b")


def test_没有上游关键帧时视频节点明确失败(mock_route, people, clean_queue):
    setup = _project(people, n_shots=1)
    _set_video_route("mlx-video", "wan2.2-i2v-a14b")
    tid = _template({"nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "shot_video", "type": "video_generate", "depends_on": ["read_project"],
         "fan_out": "shot", "params": FAST},
    ]})
    before = _count_jobs(prefix="pl-")
    view = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                               people["editor"]["id"])
    node = _nodes(view)[f"shot_video#s{setup['shot_ids'][0]}"]
    assert node["status"] == "failed"
    assert node["error_code"] == "condition_image_missing"
    assert node["job_id"] is None                # 一条注定失败的作业都不建
    assert _count_jobs(prefix="pl-") == before
    # 失败原因里说清怎么办，不只回一句"不行"
    assert "关键帧" in node["error_message"]


def _set_video_route(provider: str, model: str) -> None:
    """把视频的默认轨指到某一档。`provider_state` 负责用完还原。"""
    with get_conn() as conn:
        conn.execute("UPDATE provider_config SET is_default = 0 "
                     "WHERE capability = 'video'")
        conn.execute(
            """UPDATE provider_config
                  SET provider = ?, model = ?, enabled = 1, is_default = 1
                WHERE capability = 'video' AND backend = 'local'""",
            (provider, model))


def test_暂停_恢复_取消(mock_route, people, clean_queue):
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    _sign_doc(setup, people)

    paused = pipeline.pause_run(run_id)
    assert paused["status"] == "paused"
    pipeline.advance_run(run_id)                 # 暂停时推进是空操作
    assert _nodes(pipeline.run_view(run_id))["keyframe#s%d" % setup["shot_ids"][0]][
        "job_id"] is None
    resumed = pipeline.resume_run(run_id)
    assert resumed["status"] != "paused"
    view = _pump(run_id)
    assert _nodes(view)[f"keyframe#s{setup['shot_ids'][0]}"]["job_id"]

    # 取消：还没开始的收成 cancelled，正在跑的只发取消请求，已完成的一个不动
    pipeline.advance_run(run_id)
    cancelled = pipeline.cancel_run(run_id, "测试取消")
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_reason"] == "测试取消"
    assert cancelled["finished_at"]
    nodes = _nodes(cancelled)
    assert nodes["gate"]["status"] == "completed"            # 已完成的不改
    assert nodes[f"shot_video#s{setup['shot_ids'][0]}"]["status"] == "cancelled"
    with pytest.raises(pipeline.PipelineConflict):
        pipeline.pause_run(run_id)                           # 取消了不能再暂停
    with pytest.raises(pipeline.PipelineConflict):
        pipeline.resume_run(run_id)                          # 也不能恢复
    with pytest.raises(pipeline.PipelineConflict):
        pipeline.retry_node(nodes[f"shot_video#s{setup['shot_ids'][0]}"]["id"],
                            people["editor"]["id"])
    # 取消之后 tick 不再碰它
    assert run_id not in [r["id"] for r in pipeline_repo.tickable_runs(50)]


def test_取消会给正在跑的作业发取消请求(mock_route, people, clean_queue):
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    _sign_doc(setup, people)
    pipeline.advance_run(run_id)
    sid = setup["shot_ids"][0]
    node = _nodes(pipeline.run_view(run_id))[f"keyframe#s{sid}"]
    assert job_repo.get(node["job_id"])["status"] == "queued"
    pipeline.cancel_run(run_id, "别跑了")
    # 排队中的作业当场取消（一次供应商调用都没发生）；跑着的才是 cancel_requested
    assert job_repo.get(node["job_id"])["status"] == "cancelled"
    view = pipeline.advance_run(run_id)
    got = _nodes(view)[f"keyframe#s{sid}"]
    # 节点按 Job 的**真实状态**收，不靠猜；但"跟着运行一起被取消"记成已取消，
    # 不记成失败——那是人的决定，不是这一格跑坏了
    assert got["status"] == "cancelled"
    assert got["error_code"] == "run_cancelled"


def test_取消会收掉还在等人签字的节点(mock_route, people, clean_queue):
    """阶段 C 发现的口径缺口：human_gate 没有 job 可取消。

    不单独收的话，运行已经取消了、这一格却还挂着"等待审核"；
    更糟的是之后谁再来签一个字，`_sync_review_nodes` 会把它推成已完成——
    取消的运行被一次迟到的签字复活了。
    """
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    pipeline.advance_run(run_id)
    gate = _nodes(pipeline.run_view(run_id))["gate"]
    assert gate["status"] == "waiting_review"

    pipeline.cancel_run(run_id, "不做了")
    got = _nodes(pipeline.run_view(run_id))["gate"]
    assert got["status"] == "cancelled"
    assert "取消" in got["error_message"]

    # 迟到的签字：节点仍是已取消，运行仍是已取消
    _sign_doc(setup, people)
    view = pipeline.advance_run(run_id)
    assert _nodes(view)["gate"]["status"] == "cancelled"
    assert view["status"] == "cancelled"


def test_待人工核对不会被自动重试(mock_route, people, clean_queue):
    """云端结果未知那一档：产线只把节点摆成 attention，绝不替人再花一次钱。"""
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    _sign_doc(setup, people)
    pipeline.advance_run(run_id)
    sid = setup["shot_ids"][0]
    node = _nodes(pipeline.run_view(run_id))[f"keyframe#s{sid}"]
    with get_conn() as conn:
        conn.execute("""UPDATE job SET status = 'needs_attention',
                               attention_reason = '账单待核对' WHERE id = ?""",
                     (node["job_id"],))
    jobs_before = _count_jobs()
    view = _pump(run_id)
    node = _nodes(view)[f"keyframe#s{sid}"]
    assert node["status"] == "attention"
    assert "账单待核对" in node["error_message"]
    assert view["status"] == "attention"
    assert _count_jobs() == jobs_before            # 一条新作业都没建


def test_关联作业消失时节点报_job_missing(mock_route, people, clean_queue):
    """删掉作业行时外键把节点的 job_id 置空；不处理这一格就永远停在"执行中"。"""
    setup = _project(people, n_shots=1)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    run_id = view["id"]
    _sign_doc(setup, people)
    pipeline.advance_run(run_id)
    sid = setup["shot_ids"][0]
    node = _nodes(pipeline.run_view(run_id))[f"keyframe#s{sid}"]
    with get_conn() as conn:
        conn.execute("DELETE FROM job WHERE id = ?", (node["job_id"],))
    assert pipeline_repo.get_node(node["id"])["job_id"] is None   # SET NULL 生效
    view = pipeline.advance_run(run_id)
    got = _nodes(view)[f"keyframe#s{sid}"]
    assert got["status"] == "failed" and got["error_code"] == "job_missing"
    assert got["retry_count"] == 0                 # 报失败，不自动重试


def test_分镜缺提示词时检查节点失败(mock_route, people):
    setup = _project(people, n_shots=2, with_doc=True, prompts=False)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    nodes = _nodes(view)
    assert nodes["check_shots"]["status"] == "failed"
    assert nodes["check_shots"]["error_code"] == "shots_incomplete"
    assert "没有提示词" in nodes["check_shots"]["error_message"]
    assert nodes["gate"]["status"] == "blocked"
    assert view["status"] == "failed"

    # §14：节点失败与作业失败给的是同一张七字段目录，且由 `_node_view` 一起带下来
    # （界面再打一次接口就会问出两份答复）
    dx = nodes["check_shots"]["diagnosis"]
    assert dx["known"] is True and dx["label"]
    for field in ("explain", "technical", "impact", "action"):
        assert str(dx[field]).strip(), field
    assert isinstance(dx["retryable"], bool) and dx["may_cost"] is False
    assert dx["retryable"] is nodes["check_shots"]["retryable"], \
        "目录说可重试、闸门说不能点时，界面只能按闸门摆——两处必须给同一个答案"


def test_诊断真的从HTTP层带出来了(as_role, mock_route, people):
    """后端算好不等于界面拿得到：`response_model` 会把模型里没有的字段丢掉。
    这一条是撞过一次补的——`pipeline.run_view()` 里 diagnosis 在，
    `GET /api/pipeline/runs/{id}` 出来却没有，界面上就只剩一句执行器原话。
    """
    setup = _project(people, n_shots=2, with_doc=True, prompts=False)
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(FULL), people["editor"]["id"])
    c = as_role("editor")
    got = c.get(f"/api/pipeline/runs/{view['id']}").json()
    node = next(n for n in got["nodes"] if n["node_key"] == "check_shots")
    assert node["error_code"] == "shots_incomplete"
    assert node["diagnosis"]["known"] is True and node["diagnosis"]["label"]
    assert node["diagnosis"]["action"], "界面没有『建议做法』就等于只报了个错"""
    assert isinstance(node["diagnosis"]["retryable"], bool)
    assert node["diagnosis"]["retryable"] is node["retryable"]


def test_成片准备永远不假装完成(mock_route, people):
    """阶段 D 还没做：这一格只能是 skipped，不能写 completed。"""
    setup = _project(people, n_shots=1)
    tid = _template({"nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "check_audio", "type": "audio_prepare", "depends_on": ["read_project"]},
        {"key": "prepare_export", "type": "export_prepare", "depends_on": ["check_audio"]},
    ]})
    view = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                               people["editor"]["id"])
    nodes = _nodes(view)
    assert nodes["prepare_export"]["status"] == "skipped"
    # 音频检查只报事实、不判失败：本机没有 TTS 运行时时那不是流程错误
    assert nodes["check_audio"]["status"] == "completed"
    assert nodes["check_audio"]["output"]["clips"] == 0
    assert view["status"] == "completed_with_errors"


def test_tick_只推进还没结束的运行(mock_route, people, clean_queue):
    setup = _project(people, n_shots=1)
    other = _project(people, n_shots=1)
    tid = _template(FULL)
    running = pipeline.create_run(setup["project_id"], setup["episode_id"], tid,
                                  people["editor"]["id"])
    paused = pipeline.create_run(other["project_id"], other["episode_id"], tid,
                                 people["editor"]["id"])
    pipeline.pause_run(paused["id"])
    ids = {r["id"] for r in pipeline_repo.tickable_runs(50)}
    assert running["id"] in ids and paused["id"] not in ids

    # 两条都还停在人工确认；只给"没暂停"的那一条签字，然后靠 tick 推进
    _sign_doc(setup, people)
    _sign_doc(other, people)
    got = pipeline.tick()
    assert got["runs"] >= 1
    view = pipeline.run_view(running["id"])
    assert _nodes(view)[f"keyframe#s{setup['shot_ids'][0]}"]["job_id"]
    # 暂停那条即使签了字也不动：暂停的意思就是"别再派新的"
    stalled = pipeline.run_view(paused["id"])
    assert all(n["job_id"] is None for n in stalled["nodes"])
    assert _nodes(stalled)["gate"]["status"] == "waiting_review"


# ---------- 接口层 ----------
def test_接口权限与归属(as_role, mock_route, people, fresh_project, clean_queue):
    setup = _project(people, n_shots=1)
    tid = _template(FULL)
    editor = as_role("editor")
    r = editor.post(f"/api/projects/{setup['project_id']}/pipeline/runs",
                    json={"episode_id": setup["episode_id"], "template_id": tid})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    assert editor.get(f"/api/pipeline/runs/{run_id}").status_code == 200
    assert editor.get(f"/api/pipeline/templates").status_code == 200
    assert editor.get(f"/api/projects/{setup['project_id']}/pipeline/runs"
                      ).json()[0]["id"] == run_id
    assert editor.post(f"/api/pipeline/runs/{run_id}/advance").status_code == 200

    # viewer：读得到（job:read），发起不了（没有 job:submit ⇒ 403 先于归属判定）
    viewer_project = fresh_project("editor")
    project_repo.add_member(viewer_project, people["viewer"]["id"],
                            people["editor"]["id"])
    viewer = as_role("viewer")
    assert viewer.get("/api/pipeline/templates").status_code == 200
    assert viewer.get(f"/api/projects/{viewer_project}/pipeline/runs").status_code == 200
    assert viewer.post(f"/api/projects/{viewer_project}/pipeline/runs",
                       json={"episode_id": setup["episode_id"],
                             "template_id": tid}).status_code == 403
    assert viewer.get(f"/api/pipeline/runs/{run_id}").status_code == 404

    # 外人：一律 404，连"这条运行存在"都不透露
    outsider = as_role("outsider")
    assert outsider.get(f"/api/pipeline/runs/{run_id}").status_code == 404
    assert outsider.post(f"/api/pipeline/runs/{run_id}/cancel",
                         json={"reason": "x"}).status_code == 404
    assert outsider.get(f"/api/projects/{setup['project_id']}/pipeline/runs"
                        ).status_code == 404


def test_接口把编排异常收成正确的状态码(as_role, mock_route, people):
    setup = _project(people, n_shots=1)
    tid = _template(FULL)
    c = as_role("editor")
    r = c.post(f"/api/projects/{setup['project_id']}/pipeline/runs",
               json={"episode_id": setup["episode_id"], "template_id": tid})
    run_id = r.json()["id"]
    # 同一集再起一条 ⇒ 409
    again = c.post(f"/api/projects/{setup['project_id']}/pipeline/runs",
                   json={"episode_id": setup["episode_id"], "template_id": tid})
    assert again.status_code == 409 and "还没结束" in again.json()["detail"]
    # 模板不存在 ⇒ 404；集不属于该项目 ⇒ 422
    assert c.post(f"/api/projects/{setup['project_id']}/pipeline/runs",
                  json={"episode_id": setup["episode_id"],
                        "template_id": 999999}).status_code == 404
    other = _project(people, n_shots=1)
    assert c.post(f"/api/projects/{setup['project_id']}/pipeline/runs",
                  json={"episode_id": other["episode_id"],
                        "template_id": tid}).status_code == 422
    # 没失败的节点不给重试 ⇒ 409
    nodes = c.get(f"/api/pipeline/runs/{run_id}").json()["nodes"]
    ok = next(n for n in nodes if n["status"] == "completed")
    assert c.post(f"/api/pipeline/nodes/{ok['id']}/retry").status_code == 409


def _count_jobs(prefix: str | None = None) -> int:
    with get_conn() as conn:
        if prefix:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM job WHERE idempotency_key LIKE ?",
                (prefix + "%",)).fetchone()["n"]
        return conn.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]


# ---------- 阶段 D：成片节点（v2） ----------
# 一份最小 v2：读输入 → 合成 → 成片验收 → 成片审核。
# 合成不经过模型适配器（provider=ffmpeg），所以这份用例不切默认轨。
V2_MINI = {"nodes": [
    {"key": "read_project", "type": "project_input", "depends_on": []},
    {"key": "read_episode", "type": "episode_input", "depends_on": ["read_project"]},
    {"key": "compose", "type": "export_compose", "depends_on": ["read_episode"]},
    {"key": "check_composition", "type": "export_validate", "depends_on": ["compose"]},
    {"key": "review_composition", "type": "export_review",
     "depends_on": ["check_composition"]},
]}


@pytest.mark.skipif(not _has_ffmpeg(), reason="合成节点要真跑 FFmpeg，这台机器上没有")
def test_v2成片节点从合成到验收到审核(people, clean_queue):
    """成片节点是普通 Job：创建时冻结时间线，worker 用 FFmpeg 真跑，
    验收按成片记录复核，审核等签字。三格各自失败都不会伪装成完成。"""
    from test_composition import _clip

    setup = _project(people, n_shots=1)
    clip = _clip(setup["project_id"], setup["episode_id"], setup["shot_ids"][0],
                 people["editor"]["id"], "purple")
    _sign_job(clip["id"], setup, people)

    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(V2_MINI), people["editor"]["id"])
    compose = _nodes(view)["compose"]
    assert compose["status"] == "running"
    assert compose["job_id"]

    run = _pump(view["id"])
    nodes = _nodes(run)
    assert nodes["compose"]["status"] == "completed", nodes["compose"]["error_message"]
    assert nodes["compose"]["output"]["provider"] == "ffmpeg"
    assert nodes["check_composition"]["status"] == "completed"
    out = nodes["check_composition"]["output"]
    assert out["sha256"] and len(out["sha256"]) == 64
    # 成片跟随素材尺寸（2026-11-05 起的口径）：素材是 640×360，就不再放大到 1280×720。
    # 这一档以前写死在执行器里，等于每次合成都白白多存一倍的像素；要更大尺寸得显式填。
    assert out["width"] == 640 and out["height"] == 360
    assert run["status"] == "waiting_review"
    assert nodes["review_composition"]["status"] == "waiting_review"

    # 成片审核签字后整条运行收尾
    _sign_job(nodes["compose"]["job_id"], setup, people)
    run = _pump(view["id"])
    nodes = _nodes(run)
    assert nodes["review_composition"]["status"] == "completed"
    assert run["status"] == "completed", run["status_label"]


@pytest.mark.skipif(not _has_ffmpeg(), reason="合成前置检查要真造一条产物，靠 FFmpeg 出片")
def test_v2合成前置检查失败会点名并停在那格(people, clean_queue):
    """有片但没签字时，合成节点明确失败，不提交 Job、不产出假成片。"""
    from test_composition import _clip

    setup = _project(people, n_shots=1)
    _clip(setup["project_id"], setup["episode_id"], setup["shot_ids"][0],
          people["editor"]["id"], "orange")
    before = _count_jobs("pl-")
    view = pipeline.create_run(setup["project_id"], setup["episode_id"],
                               _template(V2_MINI), people["editor"]["id"])
    nodes = _nodes(view)
    assert nodes["compose"]["status"] == "failed"
    assert nodes["compose"]["error_code"] == "composition_precheck"
    assert "第 1 镜" in nodes["compose"]["error_message"]
    assert "审核" in nodes["compose"]["error_message"]
    assert nodes["check_composition"]["status"] == "blocked"
    assert _count_jobs("pl-") == before     # 一条合成 Job 都没排
    assert view["status"] == "failed"


def test_成片按产线签过的那条作业钉住(fresh_project, people):
    """`compose_shot_jobs` 只认产线自己冻结的作业，不回到"最新成功"。

    两档：有「镜头审核」节点时取它 input 里冻的 job_id（那是"等谁签字"）；
    只有生成节点的自定义模板退到生成节点自己挂的作业，仍然不是全库最新。
    """
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    s1 = shots.create(eid, "第一镜", "", 1.0, [])["id"]
    s2 = shots.create(eid, "第二镜", "", 1.0, [])["id"]
    template = pipeline_repo.latest_templates()[0]

    def _run() -> dict:
        return pipeline_repo.create_run(
            project_id=pid, episode_id=eid, template_id=template["id"],
            template_version=template["version"], input_snapshot={},
            idempotency_key=None, created_by=people["editor"]["id"],
            started_at="2026-11-03 10:00:00")

    run = _run()
    pipeline_repo.create_nodes(run["id"], [
        {"node_key": f"shot_video#s{s1}", "node_type": "video_generate",
         "depends_on": [], "label": "镜头视频生成"},
        {"node_key": f"review_shot#s{s1}", "node_type": "shot_review",
         "depends_on": [], "label": "镜头审核", "input": {"job_id": 101}},
        {"node_key": f"shot_video#s{s2}", "node_type": "video_generate",
         "depends_on": [], "label": "镜头视频生成"},
        {"node_key": f"review_shot#s{s2}", "node_type": "shot_review",
         "depends_on": [], "label": "镜头审核", "input": {"job_id": 202}},
    ])
    assert pipeline.compose_shot_jobs(run) == {s1: 101, s2: 202}

    # 没有审核节点时退到生成节点自己挂的作业
    run2 = _run()
    pipeline_repo.create_nodes(run2["id"], [
        {"node_key": f"shot_video#s{s1}", "node_type": "video_generate",
         "depends_on": [], "label": "镜头视频生成"},
        {"node_key": f"shot_video#s{s2}", "node_type": "video_generate",
         "depends_on": [], "label": "镜头视频生成"},
    ])
    real = [job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": shot, "capability": "video",
        "kind": "video_local", "execution_mode": "local", "provider_code": "mock",
        "model_code": "mock-1", "adapter_version": "mock", "prompt_snapshot": "",
        "params": {}, "input_refs": [], "created_by": people["editor"]["id"],
    })["id"] for shot in (s1, s2)]
    nodes = {n["node_key"]: n for n in pipeline_repo.nodes_for_run(run2["id"])}
    pipeline_repo.attach_job(nodes[f"shot_video#s{s1}"]["id"], real[0])
    pipeline_repo.attach_job(nodes[f"shot_video#s{s2}"]["id"], real[1])
    assert pipeline.compose_shot_jobs(run2) == {s1: real[0], s2: real[1]}


def test_生产前检查_阻断与警告分开(fresh_project, people):
    """预检只做确定性判断：缺资产是阻断，空提示词是警告。

    这条用例同时钉住"预检结果真的会挡住发起"：blocking 不为空时 create_run 抛
    PipelineInvalid，而不是先建运行再让第一格失败。
    """
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shots.create(eid, "第一镜", "有提示词", 1.0, [])
    shots.create(eid, "第二镜", "", 1.0, [999999])
    template = next(t for t in pipeline_repo.latest_templates()
                    if t["name"] == "标准漫剧单集生产" and t["version"] == 2)

    check = pipeline.preflight(pid, eid, template["id"])
    assert any("资产 #999999 不存在" in item for item in check["blocking"])
    assert any("第 2 镜没有提示词" in item for item in check["warnings"])
    assert not any("第 1 镜没有提示词" in item for item in check["warnings"])
    assert check["info"]["shots"] == 2

    with pytest.raises(pipeline.PipelineInvalid) as exc:
        pipeline.create_run(pid, eid, template["id"], people["editor"]["id"])
    assert "生产前检查没过" in str(exc.value)


def test_没有参考图的资产要警告(fresh_project):
    """`ref_images` 存的是 JSON 文本，空清单是 "[]"——非空字符串。

    v0.96 真浏览器验收抓到这条警告从来没说过话：`if not row["ref_images"]` 对
    "[]" 永远为假，界面因此少了一寸"定妆一致性没有依据"的提醒。
    """
    pid = fresh_project("editor")
    from repositories.content import assets
    eid = episodes.create(pid, "第 1 集")["id"]
    bare = assets.create(project_id=pid, kind="character", name="没定妆",
                         summary="", consistency_prompt="银发")["id"]
    assert json.loads(assets.get(bare)["ref_images"]) == []
    shots.create(eid, "第一镜", "推门", 1.0, [bare])
    template = next(t for t in pipeline_repo.latest_templates()
                    if t["name"] == "标准漫剧单集生产" and t["version"] == 2)

    warnings = pipeline.preflight(pid, eid, template["id"])["warnings"]
    assert any("没定妆" in item and "没有参考图" in item for item in warnings)
    # 有一致性提示词，就不该同时报"没有一致性提示词"
    assert not any("没定妆" in item and "一致性提示词" in item for item in warnings)


def test_预检接口在发起前给出清单(as_role, fresh_project):
    """`GET /projects/{id}/preflight`：看得见的人拿清单，看不见的人 404。"""
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shots.create(eid, "第一镜", "", 1.0, [999999])
    template = next(t for t in pipeline_repo.latest_templates()
                    if t["name"] == "标准漫剧单集生产" and t["version"] == 2)

    resp = c.get(f"/api/projects/{pid}/preflight"
                 f"?episode_id={eid}&template_id={template['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert any("资产 #999999 不存在" in item for item in data["blocking"])
    assert any("没有提示词" in item for item in data["warnings"])
    assert data["info"]["shots"] == 1

    outsider = as_role("outsider")
    assert outsider.get(f"/api/projects/{pid}/preflight?episode_id={eid}"
                        ).status_code == 404


def test_整集清单的句子按里面的具体物认跳转():
    """`_episode_locate()` 是「产线弹窗那一行字 → 跳到哪儿」的唯一一处翻译。
    它按关键词认，所以用例直接拿 `preflight()` 真实会说的那几句原文来钉：
    顺序错了（「资产 X 没有参考图」被「资产」抢先）就会把人丢去列表页而不是补图的地方。
    """
    rows = [{"id": 41, "seq": 1}, {"id": 42, "seq": 2}]
    assert precheck._episode_locate("第 2 镜没有提示词，生成时会拿内容那一栏代替", rows) == {
        "module": "shot", "sub": 0, "shot_id": 42, "label": "第 2 镜"}
    # 引用不存在的资产：那句话里同时有「资产」与「第 1／2 镜」——坏引用住在镜头上，
    # 所以先落到那一镜，而不是把人在资产列表里自己找
    assert precheck._episode_locate("资产 #999999 不存在，第 1／2 镜还在引用它", rows)[
        "shot_id"] == 41
    assert precheck._episode_locate("资产「燧人」没有参考图，定妆一致性没有依据", rows) == {
        "module": "assets", "sub": 1}
    assert precheck._episode_locate("video 没有可用的默认轨：这一档没有登记", rows) == {
        "module": "models", "sub": 1}
    assert precheck._episode_locate("生成 worker 不在线；运行建好了也不会自己往前走", rows) == {
        "module": "generate", "sub": 0}
    assert precheck._episode_locate("这一集已有一条未结束的运行（#7）；先取消或等它跑完", rows) == {
        "module": "pipeline", "sub": 0}
    assert precheck._episode_locate("这一集还没有镜头；先去创作中心把分镜建起来", rows) == {
        "module": "writing", "sub": 2}
    # 认不出来就不摆按钮：一句没头没尾的话硬给个默认页，点了只会跳错地方
    assert precheck._episode_locate("说不清是什么的事", rows) == {}


def test_发起弹窗读的是统一预检那一份结论(as_role, fresh_project):
    """任务书 §10 的「产线与人工两种模式共用一套」落到界面上就是一句话：
    发起弹窗问的接口（`/api/projects/{id}/precheck`）内部就是 `pipeline.preflight`，
    但它多给了三档条目、跳转与 `can_start`——按钮禁不禁只认服务端那一格布尔。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    shot = shots.create(eid, "第一镜", "", 1.0, [999999])
    template = next(t for t in pipeline_repo.latest_templates()
                    if t["name"] == "标准漫剧单集生产" and t["version"] == 2)
    data = c.get(f"/api/projects/{pid}/precheck"
                 f"?episode_id={eid}&template_id={template['id']}").json()
    assert data["status"] == "阻塞" and data["can_start"] is False
    assert data["blocking_texts"] and data["warning_texts"] is not None
    bad = [i for i in data["items"] if i["level"] == "阻塞"]
    assert any("资产 #999999 不存在" in i["text"] for i in bad)
    # 每条都能指出去处（或明说认不出＝空字典），界面不用再猜一遍
    for item in data["items"]:
        assert isinstance(item["locate"], dict) and item["code"].startswith("pipeline.")
    jumped = [i for i in bad if i["locate"].get("shot_id") == int(shot["id"])]
    assert jumped, "阻塞项里那条坏引用该跳到这一镜的工作台"


def test_派发中断_节点落成可重试的失败(mock_route, people):
    """两阶段派发：事务里只占格，建 Job 在写锁外。

    模拟「事务提交了、进程在建单前挂掉」：节点应当停在 running 且没有 job_id；
    下一次推进由 `_sync_job_nodes` 明确判成 job_missing（可重试）——
    这是可见、可恢复的失败，好过「Job 建好了、节点却不知道」那样重复扣费。
    """
    setup = _project(people, n_shots=1)
    run_id = pipeline.create_run(setup["project_id"], setup["episode_id"],
                                 _template(FULL), people["editor"]["id"])["id"]
    run = pipeline_repo.get_run(run_id)
    nodes = pipeline_repo.nodes_for_run(run_id)
    key = f"keyframe#s{setup['shot_ids'][0]}"
    node = dict(next(n for n in nodes if n["node_key"] == key))

    # 造「依赖已完成」：真实路径里这一格由 gate 签字放出，这里只测派发本身。
    with get_conn() as conn:
        conn.execute("UPDATE pipeline_node_run SET status = 'pending' WHERE id = ?",
                     (node["id"],))

    pending: list = []
    pipeline._submit_job_node(run, node, pipeline._node_map(nodes), pending=pending)
    assert len(pending) == 1, "事务内只该收集请求，不该建 Job"

    fresh = {n["node_key"]: n for n in pipeline_repo.nodes_for_run(run_id)}[key]
    assert fresh["status"] == "running" and fresh["job_id"] is None

    # 进程「崩溃」：没人执行 pending 里的提交。下一拍必须明说这一格要重试。
    pipeline._sync_job_nodes(run, pipeline_repo.nodes_for_run(run_id))
    fresh = {n["node_key"]: n for n in pipeline_repo.nodes_for_run(run_id)}[key]
    assert fresh["status"] == "failed"
    assert fresh["error_code"] == "job_missing"
