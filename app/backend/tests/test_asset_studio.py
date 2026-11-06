"""资产中心增强包（迁移 0033）：人物档案、命名形态、资产生成与产物导入。

三条测试纪律，和这一包的设计纪律一一对应：

- **兼容性优先**：老资产升级后必须照常打得开；档案读不到就是"还没写"，不是缺数据。
- **不演成功**：作业没成功、模型没按标记分段、产物不是图片——三种情况都必须明确
  失败，不能把半成品填进表单或写进图清单。
- **边界用真文件真接口**：导入产物这条路走的是真的 `jobfiles` 与真的磁盘文件。
  假在内存里的路径测不出"客户端能不能指到门外"。

产物这一侧要说明白：测试用 `job_repo.succeed()` 登记一个**真的落在临时目录里的
PNG 文件**，而不是让 mock 适配器去生图——mock 生不出 PNG 字节。这里测的是导入边界
（可见性、状态、能力、序号、文件头、路径），不是"模型能不能画"。真实模型能不能
产出这张图，仍然只有真机那一轮才算（本轮 `model_validation` 一条没加）。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from types import SimpleNamespace

import asset_studio
import jobfiles
import migrate
import pytest
from config import FILES_DIR
from db import get_conn
from repositories.content import assets as asset_repo
from repositories.content import profiles, variants
from repositories.jobs import jobs as job_repo

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")

# 一个不认识的 operation 的桩：只为验第一道校验，不给它任何真实内容
BAD_OP = SimpleNamespace(
    operation="no_such_op", target_asset_id=None, target_variant_id=None,
    instruction="", image_prompt="", draft_name="", draft_summary="",
    draft_consistency_prompt="", execution_mode="local", provider_code="mock",
    model_code="mock-1", params={}, estimate=None, user_confirmed_cloud=False,
    idempotency_key=None)


def _new_asset(pid, kind="character"):
    return asset_repo.create(pid, kind, f"{kind}_{uuid.uuid4().hex[:5]}", "", "", [])["id"]


def _make_job(pid, capability, refs, *, worker="w-studio", state="succeeded", files=None):
    """造一条处在 succeeded / failed / queued 三种状态之一的作业。

    `files` 是 {产物文件名: 字节}：**先落盘、再登记**——`jobs.succeed()` 只认
    已经存在的文件，先 succeed 再补写会得到一条产物清单为空的作业（第一版就踩过）。

    两个讲究：
    - 先把自己以外排队中的作业收掉再领：`claim_next` 领的是最旧那条，
      不清干净就领不到本用例这一条（conftest 的 seeded 同一做法）；
    - **不把作业留在 running**。并发上限默认是"全局 1"，一条留在 running 的作业
      会把后面所有用例的领取挡死。要"还没做完"就用 queued——它不占槽，
      语义上也正是"排着队还没跑"。

    领取与收尾都走仓储层的真方法，不裸写 UPDATE：状态机本身由 test_jobs.py 覆盖，
    这里要的是"一条真在队列里走到某个终态的作业"。
    """
    with get_conn() as conn:
        conn.execute("""UPDATE job SET status = 'cancelled',
                               finished_at = datetime('now','localtime'),
                               error_code = 'test_cleanup'
                        WHERE status = 'queued'""")
    job = job_repo.create({
        "project_id": pid, "episode_id": None, "shot_id": None,
        "capability": capability, "kind": capability, "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": "测试用", "params": {}, "input_refs": refs,
        "created_by": None,
    })
    if state == "queued":
        return job_repo.get(job["id"])          # 不领：排队中，不占并发槽

    rels = []
    for name, content in (files or {}).items():
        path = jobfiles.job_dir(job["id"]) / "outputs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        rels.append(jobfiles.relative(path))

    claimed = job_repo.claim_next(worker, 60)
    assert claimed is not None and claimed["id"] == job["id"], "领取到的不是本用例那条"
    job_repo.set_running(job["id"], worker)
    if state == "succeeded":
        job_repo.succeed(job["id"], worker, "", rels, 0)
    else:
        job_repo.fail(job["id"], worker, "test_failed", "测试用失败")
    return job_repo.get(job["id"])


def _image_job(pid, *, content=PNG, name="frame.png", state="succeeded"):
    """一条有真实 PNG 产物的图像作业。"""
    return _make_job(pid, "image", ["op:asset_image"], state=state,
                     files={name: content}), None


def _text_job(pid, body: str, operation="character_profile"):
    """一条文本产物按给定文字落盘的文本作业。"""
    return _make_job(pid, "text", [f"op:{operation}"], state="succeeded",
                     files={"result.txt": body.encode("utf-8")})


@pytest.fixture
def studio(fresh_project):
    """本文件专用的项目与人物资产。

    不在会话共用的 `seeded` 项目里堆资产：test_dashboard 断言"这个项目恰好 1 条资产"、
    test_projects 断言"它的参考图计数是 0"，我在这儿每建一条就把那两个数改了一次。
    共享 fixture 的计数属于大家，谁加谁负责。
    """
    pid = fresh_project("editor")
    aid = asset_repo.create(pid, "character", f"人物_{uuid.uuid4().hex[:5]}", "", "", [])["id"]
    return {"pid": pid, "aid": aid}


def _queued_count(pid) -> int:
    """只数**排队中**的作业：本文件会留下若干已收尾的作业做证据，
    数全项目的话每个用例的基线都不一样。"""
    with get_conn() as conn:
        return conn.execute("""SELECT COUNT(*) AS n FROM job
                               WHERE project_id = ? AND status = 'queued'""",
                            (pid,)).fetchone()["n"]


# ---------- 兼容性与人物档案 ----------

def test_老资产升级后照常打得开且档案是空的不是缺数据(as_role, studio):
    """0033 只加两张表，`asset` 一列都没动——这是升级的底线。"""
    c = as_role("editor")
    assert c.get(f"/api/assets/{studio['aid']}").status_code == 200

    p = c.get(f"/api/assets/{studio['aid']}/character-profile")
    assert p.status_code == 200, p.text
    body = p.json()
    assert body["exists"] is False
    assert body["biography"] == "" and body["text_prompt"] == ""


def test_档案整体保存后读回一致(as_role, studio):
    c = as_role("editor")
    url = f"/api/assets/{studio['aid']}/character-profile"
    r = c.put(url, json={"biography": "山下铁匠家长大", "personality": "话少，认死理",
                         "growth_arc": "从守炉到守村", "text_prompt": "别写超能力"})
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is True
    assert r.json()["biography"] == "山下铁匠家长大"
    assert c.get(url).json()["personality"] == "话少，认死理"
    assert c.get(url).json()["updated_at"]


def test_非人物资产没有档案这一层(as_role, studio):
    """回 422 而不是回一份空档案：装成"有但空"会让人去找那个不存在的编辑入口。"""
    c = as_role("editor")
    scene = _new_asset(studio["pid"], "scene")
    assert c.get(f"/api/assets/{scene}/character-profile").status_code == 422
    assert c.put(f"/api/assets/{scene}/character-profile",
                 json={"biography": "不该写进去"}).status_code == 422
    assert profiles.get(scene)["exists"] is False


# ---------- 命名形态 ----------

def test_形态可建可改可排(as_role, studio):
    c = as_role("editor")
    aid = _new_asset(studio["pid"])      # 自己的资产：形态名不能撞别人留下的
    made = c.post(f"/api/assets/{aid}/variants",
                  json={"variant_type": "outfit", "name": "雨夜外套", "prompt": "湿反光"})
    assert made.status_code == 201, made.text
    vid = made.json()["id"]
    assert made.json()["variant_type_label"] == "服饰"

    upd = c.put(f"/api/asset-variants/{vid}",
                json={"variant_type": "outfit", "name": "雨夜外套", "prompt": "湿反光＋霓虹",
                      "sort_order": 3})
    assert upd.status_code == 200 and upd.json()["sort_order"] == 3
    assert upd.json()["prompt"] == "湿反光＋霓虹"
    listed = c.get(f"/api/assets/{aid}/variants").json()
    assert [v["id"] for v in listed] == [vid]


def test_同名形态回人可以改不自动加序号(as_role, studio):
    """前端悄悄改名再说"已保存"，人拿到的就不是他打的那个名字。"""
    c = as_role("editor")
    aid = _new_asset(studio["pid"])
    body = {"variant_type": "outfit", "name": "常服"}
    assert c.post(f"/api/assets/{aid}/variants", json=body).status_code == 201
    dup = c.post(f"/api/assets/{aid}/variants", json=body)
    assert dup.status_code == 409 and "常服" in dup.json()["detail"]
    assert c.post(f"/api/assets/{aid}/variants",
                  json={"variant_type": "outfit", "name": "常服二"}).status_code == 201


def test_形态类型必须与资产类型对得上(as_role, studio):
    """DDL 的 CHECK 只能收死取值；"outfit 只能挂人物"这条跨表规则在服务层判。"""
    c = as_role("editor")
    scene = _new_asset(studio["pid"], "scene")
    assert c.post(f"/api/assets/{scene}/variants",
                  json={"variant_type": "outfit", "name": "错挂的衣服"}).status_code == 422
    assert c.post(f"/api/assets/{scene}/variants",
                  json={"variant_type": "scene_view", "name": "俯瞰"}).status_code == 201


def test_形态可见性从父资产继承(as_role, studio):
    """只校验"variant id 查得到"是不够的：那等于任何人都能读改别人的形态。"""
    vid = variants.create(asset_id=studio["aid"], variant_type="outfit",
                          name="内衬")["id"]
    outsider = as_role("outsider")
    assert outsider.get(f"/api/assets/{studio['aid']}/variants").status_code == 404
    assert outsider.put(f"/api/asset-variants/{vid}",
                        json={"variant_type": "outfit", "name": "改掉", "prompt": "",
                              "sort_order": 0}).status_code == 404
    assert outsider.delete(f"/api/asset-variants/{vid}").status_code == 404
    assert variants.get(vid)["name"] == "内衬"


def test_删形态把记录清掉(as_role, studio):
    c = as_role("editor")
    # 用本用例自己的资产：seeded 那条是会话内共用的，别的用例已经在上面建过形态，
    # 拿它断言"清单空了"测的就不是删除，而是用例顺序
    aid = _new_asset(studio["pid"])
    vid = variants.create(asset_id=aid, variant_type="outfit", name="旅装")["id"]
    variants.add_image(vid, f"{aid}/not-on-disk.png")     # 库里登记过、盘上没有
    assert c.delete(f"/api/asset-variants/{vid}").status_code == 204
    assert variants.get(vid) is None
    assert variants.list_by_asset(aid) == []


def test_全局资产的形态写入要管理员(as_role, studio):
    g = asset_repo.create(None, "character", f"全局_{uuid.uuid4().hex[:5]}", "", "", [])["id"]
    assert as_role("editor").get(f"/api/assets/{g}/variants").status_code == 200  # 人人可读
    assert as_role("editor").post(f"/api/assets/{g}/variants",
                                  json={"variant_type": "outfit",
                                        "name": "不该建"}).status_code == 403
    assert as_role("admin").post(f"/api/assets/{g}/variants",
                                 json={"variant_type": "outfit",
                                       "name": "管理员建的"}).status_code == 201


# ---------- 提交生成作业：只走那一条提交路 ----------

def test_文字设定作业排进同一个队列(as_role, studio):
    c = as_role("editor")
    before = _queued_count(studio["pid"])
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "character_profile", "target_asset_id": studio["aid"],
        "instruction": "别写超能力", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "params": {"sleep": 0.01}})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["capability"] == "text"
    assert body["operation_label"] == "人物文字设定"
    assert _queued_count(studio["pid"]) == before + 1

    job = job_repo.get(body["job_id"])
    refs = json.loads(job["input_refs_json"])
    assert f"op:character_profile" in refs
    assert f"asset:{studio['aid']}" in refs
    # 提示词冻进快照：事后能回答"当时喂了模型什么"
    assert "别写超能力" in job["prompt_snapshot"]
    assert job["episode_id"] is None and job["shot_id"] is None


def test_空图像提示词不建作业(as_role, studio):
    c = as_role("editor")
    before = _queued_count(studio["pid"])
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "asset_image", "image_prompt": "   ", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "params": {"sleep": 0.01}})
    assert r.status_code == 422, r.text
    assert _queued_count(studio["pid"]) == before       # 队列里一条没多


def test_目标资产与形态必须对得上(as_role, studio):
    c = as_role("editor")
    other = _new_asset(studio["pid"], "character")
    vid = variants.create(asset_id=other, variant_type="outfit", name="别人的衣服")["id"]
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "asset_image", "image_prompt": "一张图",
        "target_asset_id": studio["aid"], "target_variant_id": vid,
        "execution_mode": "local", "provider_code": "mock", "model_code": "mock-1",
        "params": {"sleep": 0.01}})
    assert r.status_code == 422 and "不属于" in r.json()["detail"]

    scene = _new_asset(studio["pid"], "scene")
    assert c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "character_profile", "target_asset_id": scene,
        "execution_mode": "local", "provider_code": "mock", "model_code": "mock-1",
        "params": {"sleep": 0.01}}).status_code == 422


def test_还没保存的新资产也能先生成(as_role, studio):
    """草稿路径：作业只记项目，不记资产 id；权限等真保存时那一道再判。"""
    c = as_role("editor")
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "character_profile", "draft_name": "还没建的人",
        "draft_summary": "守夜人", "instruction": "写得克制一点",
        "execution_mode": "local", "provider_code": "mock", "model_code": "mock-1",
        "params": {"sleep": 0.01}})
    assert r.status_code == 202, r.text
    assert r.json()["target_asset_id"] is None
    job = job_repo.get(r.json()["job_id"])
    assert "还没建的人" in job["prompt_snapshot"]
    assert "asset:" not in json.loads(job["input_refs_json"])[0]


def test_云端没勾确认不建作业(as_role, studio):
    """没勾就不该有一次请求发出去，也不该在账上留下一行。"""
    c = as_role("editor")
    before = _queued_count(studio["pid"])
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "asset_image", "image_prompt": "一张图", "execution_mode": "cloud",
        "provider_code": "wanx", "model_code": "wan2.7-image",
        "estimate": {"quantity": 1, "unit": "image", "unit_price_micros": 200000,
                     "currency": "CNY", "source": "清单"}})
    assert r.status_code == 422 and "先勾" in r.json()["detail"]
    assert _queued_count(studio["pid"]) == before


def test_云端单价与清单不一致就拒(as_role, studio, provider_state):
    """勾了确认也不行：单价必须是清单登记的那一个，客户端报什么价都不采信。"""
    with get_conn() as conn:
        conn.execute("""UPDATE provider_config SET provider='deepseek',
                               model='deepseek-flash', enabled=1
                        WHERE capability='text' AND backend='cloud'""")
    c = as_role("editor")
    before = _queued_count(studio["pid"])
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "character_profile", "target_asset_id": studio["aid"],
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "user_confirmed_cloud": True,
        "estimate": {"quantity": 0.001, "unit": "million_token", "unit_price_micros": 1,
                     "currency": "CNY", "source": "瞎填"}})
    assert r.status_code == 422 and "单价" in r.json()["detail"]
    assert _queued_count(studio["pid"]) == before   # 一条都没多


def test_单价对得上就按清单重建那张单子(as_role, studio, provider_state):
    """正例：与清单一致的价格 + 勾了确认 → 建作业，成本由服务端算。"""
    with get_conn() as conn:
        conn.execute("""UPDATE provider_config SET provider='deepseek',
                               model='deepseek-flash', enabled=1
                        WHERE capability='text' AND backend='cloud'""")
    c = as_role("editor")
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "character_profile", "target_asset_id": studio["aid"],
        "execution_mode": "cloud", "provider_code": "deepseek",
        "model_code": "deepseek-flash", "user_confirmed_cloud": True,
        "params": {"max_tokens": 2048},
        "estimate": {"quantity": 0.002, "unit": "million_token",
                     "unit_price_micros": 8_000_000, "currency": "CNY",
                     "source": "DeepSeek 官方定价"}})
    assert r.status_code == 202, r.text
    job = job_repo.get(r.json()["job_id"])
    assert job["cost_micros"] == 16000                    # 0.002 × 8,000,000
    assert job["billable_status"] == "pending_reconciliation"


def test_没登记计价的云端模型档不提交计费作业(as_role, studio):
    c = as_role("editor")
    r = c.post(f"/api/projects/{studio['pid']}/asset-generations", json={
        "operation": "asset_image", "image_prompt": "一张图", "execution_mode": "cloud",
        "provider_code": "wanx", "model_code": "wan2.7-image",
        "user_confirmed_cloud": True,
        "estimate": {"quantity": 1, "unit": "image", "unit_price_micros": 200000,
                     "currency": "CNY", "source": "清单"}})
    assert r.status_code == 422, r.text
    assert "计价" in r.json()["detail"] or "不在清单" in r.json()["detail"]


# ---------- 结果预览：不演成功 ----------

def test_预览按标记分段填草稿(as_role, studio):
    job = _text_job(studio["pid"],
                    "【履历】山下铁匠家长大。\n【性格】话少，认死理。\n"
                    "【成长过程】从守炉到守村。\n【一致性提示词】左眉有一道旧疤。")
    body = as_role("editor").get(
        f"/api/projects/{studio['pid']}/asset-generations/{job['id']}").json()
    assert body["can_fill"] is True
    assert body["fields"]["biography"] == "山下铁匠家长大。"
    assert body["fields"]["consistency_prompt"] == "左眉有一道旧疤。"
    assert "核对" in body["note"]


def test_少一段就明说少的是哪段(as_role, studio):
    job = _text_job(studio["pid"], "【履历】山下铁匠家长大。\n【性格】话少。")
    body = as_role("editor").get(
        f"/api/projects/{studio['pid']}/asset-generations/{job['id']}").json()
    assert body["can_fill"] is True
    assert "成长过程" in body["note"] and "一致性提示词" in body["note"]


def test_模型没按标记分段时不自动填表(as_role, studio):
    """把整坨文字塞进第一个字段冒充成功，是最容易骗到人的一种失败。"""
    job = _text_job(studio["pid"], "这是一段没有任何标记的散文。")
    body = as_role("editor").get(
        f"/api/projects/{studio['pid']}/asset-generations/{job['id']}").json()
    assert body["can_fill"] is False
    assert body["fields"] == {}
    assert "散文" in body["raw"]


def test_作业没成功时预览不给出可填字段(as_role, studio):
    job = _make_job(studio["pid"], "text", ["op:character_profile"], state="queued")
    body = as_role("editor").get(
        f"/api/projects/{studio['pid']}/asset-generations/{job['id']}").json()
    assert body["can_fill"] is False and body["fields"] == {}
    assert body["status"] == "queued"


def test_失败作业的原文错误一起带回(as_role, studio):
    job = _make_job(studio["pid"], "text", ["op:character_profile"], state="failed")
    body = as_role("editor").get(
        f"/api/projects/{studio['pid']}/asset-generations/{job['id']}").json()
    assert body["can_fill"] is False
    assert body["error_code"] == "test_failed"


def test_预览只认资产中心发起的作业(as_role, studio):
    job = _make_job(studio["pid"], "text", ["doc:1"], state="succeeded")
    r = as_role("editor").get(
        f"/api/projects/{studio['pid']}/asset-generations/{job['id']}")
    assert r.status_code == 422, r.text


def test_预览不跨项目读别人的作业(as_role, studio, fresh_project):
    mine = _text_job(studio["pid"], "【履历】我的。")
    other_pid = fresh_project("editor")
    r = as_role("editor").get(
        f"/api/projects/{other_pid}/asset-generations/{mine['id']}")
    assert r.status_code == 422, r.text


def test_图像作业的状态端点不装成文字分段(as_role, studio):
    """轮询对文字与图像用的是同一个端点。

    这一支要是漏了，`parse_sections('asset_image')` 会在 OUTPUT_SPEC 上 KeyError，
    界面看到的是"候选一直不出来"＋一条 500——上一轮真浏览器验收就是这么撞上的。
    写死成 200＋一句人话，才算把这条边界钉住。
    """
    refs = asset_studio.build_refs(asset_studio.IMAGE_OPERATION, studio["aid"], None)
    job = _make_job(studio["pid"], "image", refs, state="succeeded",
                    files={"result.txt": "我不是图片".encode("utf-8")})
    r = as_role("editor").get(
        f"/api/projects/{studio['pid']}/asset-generations/{job['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_fill"] is False and body["fields"] == {}
    assert "候选" in body["note"]


def test_演示开关打开时清单给假槽位关掉就没有(as_role, monkeypatch):
    """`providers/available` 里那条 mock 是**演示/自测开关**的产物。

    它存在的理由只有一个：这台机器上文本轨被强制走云端、云端图像档没登记计价，
    "提交→领取→回稿→填表"这条链在真浏览器里否则一步都走不通。
    开关关掉（＝生产默认）时清单里必须一个 mock 字都没有。
    """
    c = as_role("editor")
    rows = c.get("/api/providers/available").json()
    mock_rows = [r for r in rows if r["provider"] == "mock"]
    assert mock_rows, "ALLOW_MOCK=1 时清单该有假槽位，界面才走得出这条链"
    assert {r["capability"] for r in mock_rows} >= {"text", "image"}
    assert all(r["backend"] == "local" for r in mock_rows)
    assert all("不执行模型" in r["adapter_label"] for r in mock_rows)
    assert all(r["models"] and r["models"][0]["params"] for r in mock_rows)

    monkeypatch.setattr("routers.settings.ALLOW_MOCK", False)
    rows = c.get("/api/providers/available").json()
    assert not [r for r in rows if r["provider"] == "mock"]


# ---------- 从成功作业产物导入资产图：边界 ----------

def test_导入产物进基础参考图并保留原产物(as_role, studio):
    job, rel = _image_job(studio["pid"])
    c = as_role("editor")
    r = c.post(f"/api/assets/{studio['aid']}/generated-images",
               json={"job_id": job["id"], "artifact_index": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["image_count"] == 1 and body["variant_id"] is None
    assert body["path"].startswith(f"{studio['aid']}/")

    # 复制而不是移动：原产物还在——产物审核签的是"路径＋存在＋字节数"，
    # 搬走它等于偷偷让已经签过字的清单变成空目录
    assert jobfiles.artifact_at(job_repo.get(job["id"])["result_paths_json"], 0).is_file()
    assert json.loads(asset_repo.get(studio["aid"])["ref_images"]) == body["ref_images"]
    assert (FILES_DIR / body["path"]).is_file()


def test_导入进形态并记下来源作业(as_role, studio):
    job, _ = _image_job(studio["pid"])
    aid = _new_asset(studio["pid"])
    vid = variants.create(asset_id=aid, variant_type="outfit", name="常服")["id"]
    r = as_role("editor").post(f"/api/assets/{aid}/generated-images",
                               json={"job_id": job["id"], "artifact_index": 0,
                                     "variant_id": vid})
    assert r.status_code == 200, r.text
    row = variants.get(vid)
    assert json.loads(row["ref_images"]) == r.json()["ref_images"]
    assert row["source_job_id"] == job["id"]
    # 基础参考图不受影响：服饰形态不该被下游当成同一组参考图
    assert json.loads(asset_repo.get(aid)["ref_images"]) == []


def test_不是图像或没成功的作业不能导入(as_role, studio):
    c = as_role("editor")
    text_job = _text_job(studio["pid"], "【履历】文字作业不能当图存。")
    r = c.post(f"/api/assets/{studio['aid']}/generated-images",
               json={"job_id": text_job["id"], "artifact_index": 0})
    assert r.status_code == 409 and "图像" in r.json()["detail"]

    pending = _make_job(studio["pid"], "image", ["op:asset_image"], state="queued")
    r2 = c.post(f"/api/assets/{studio['aid']}/generated-images",
                json={"job_id": pending["id"], "artifact_index": 0})
    assert r2.status_code == 409, r2.text


def test_序号越界与文件已被清理都回人话(as_role, studio):
    c = as_role("editor")
    job, _ = _image_job(studio["pid"])
    r = c.post(f"/api/assets/{studio['aid']}/generated-images",
               json={"job_id": job["id"], "artifact_index": 7})
    assert r.status_code == 422, r.text

    (jobfiles.job_dir(job["id"]) / "outputs" / "frame.png").unlink()
    r2 = c.post(f"/api/assets/{studio['aid']}/generated-images",
                json={"job_id": job["id"], "artifact_index": 0})
    assert r2.status_code == 422 and "不存在" in r2.json()["detail"]


def test_产物不是图片就不当参考图存(as_role, studio):
    """文件头说了算：清单里那条路径指向什么字节，就按什么字节判。"""
    aid = _new_asset(studio["pid"])     # 自己的资产，不读别人导入过的清单
    job, _ = _image_job(studio["pid"], content=b"RIFF....not really a webp",
                        name="fake.png")
    r = as_role("editor").post(f"/api/assets/{aid}/generated-images",
                               json={"job_id": job["id"], "artifact_index": 0})
    assert r.status_code == 415, r.text
    assert json.loads(asset_repo.get(aid)["ref_images"]) == []


def test_导入不接受任何路径字段(as_role, studio):
    """多传一个 path 不改变行为：入参模型里没有这一项，路径只能由服务端查出来。"""
    job, _ = _image_job(studio["pid"])
    r = as_role("editor").post(f"/api/assets/{studio['aid']}/generated-images",
                               json={"job_id": job["id"], "artifact_index": 0,
                                     "path": "../../etc/passwd"})
    assert r.status_code == 200, r.text
    assert ".." not in r.json()["path"]
    assert (FILES_DIR / r.json()["path"]).is_file()


def test_形态不属于这条资产时拒(as_role, studio):
    job, _ = _image_job(studio["pid"])
    other = _new_asset(studio["pid"], "character")
    vid = variants.create(asset_id=other, variant_type="outfit", name="另一条的")["id"]
    r = as_role("editor").post(f"/api/assets/{studio['aid']}/generated-images",
                               json={"job_id": job["id"], "artifact_index": 0,
                                     "variant_id": vid})
    assert r.status_code == 409, r.text


def test_两次导入不互相覆盖(as_role, studio):
    """ref_images 是读-改-写；路由套了事务，两个人同时存图应该两张都在。"""
    aid = _new_asset(studio["pid"])
    job_a, _ = _image_job(studio["pid"])
    job_b, _ = _image_job(studio["pid"])
    c = as_role("editor")
    for jid in (job_a["id"], job_b["id"]):
        assert c.post(f"/api/assets/{aid}/generated-images",
                      json={"job_id": jid, "artifact_index": 0}).status_code == 200
    images = json.loads(asset_repo.get(aid)["ref_images"])
    assert len(images) == 2 and len(set(images)) == 2


def test_删资产把形态图一起清掉(fresh_project, people, delete_guard):
    """0033 之后 `projects.asset_images()` 不再是一份完整清单——
    漏了这一处，形态图就会变成没人认领的孤儿文件。"""
    import services
    pid = fresh_project("editor")
    aid = _new_asset(pid)
    vid = variants.create(asset_id=aid, variant_type="outfit", name="旅装")["id"]
    path = FILES_DIR / str(aid) / "variant.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    variants.add_image(vid, f"{aid}/variant.png")

    services.delete_project_cascade(pid)
    assert not path.exists(), "形态名下的图没被清掉"
    assert variants.get(vid) is None


# ---------- 迁移与仓储 ----------

def test_迁移可重跑且新表是空的():
    """升级不回填：这两张表建出来就该是空的，第二次跑也不加东西也不报错。"""
    assert migrate.migrate(verbose=False) == 0
    assert profiles.get(10 ** 9)["exists"] is False
    assert variants.list_by_asset(10 ** 9) == []


def test_仓储拒绝把形态挂到不存在的资产上():
    """外键是真的在管（get_conn 里 PRAGMA foreign_keys=ON），报错要报得出来。"""
    with pytest.raises(sqlite3.IntegrityError):
        variants.create(asset_id=10 ** 9, variant_type="outfit", name="悬空的")


def test_服务层不认识的操作直接拒():
    with pytest.raises(asset_studio.AssetGenInvalid):
        asset_studio.submit_generation(1, BAD_OP, None)


def test_段中间的标记不算段首():
    """模型偶尔会在履历里写"他说【性格】其实很稳"。那种行中间的出现不能当段首，
    否则后面真正的性格段会被吃掉——留空只是少填一格，吃错段是把话填错人。"""
    out = asset_studio.parse_sections("character_profile",
                                      "【履历】他说【性格】其实很稳。\n【性格】认死理。\n"
                                      "【成长过程】从守炉到守村。")
    assert out["biography"] == "他说【性格】其实很稳。"
    assert out["personality"] == "认死理。"
    assert out["growth_arc"] == "从守炉到守村。"
    assert "consistency_prompt" not in out


# ---------- v0.99：幂等、任务模式、版本冲突与形态图片 ----------

GEN = {"execution_mode": "local", "provider_code": "mock", "model_code": "mock-1",
       "params": {"sleep": 0.01}}


def _gen(c, pid, **extra):
    body = dict(GEN)
    body.update(extra)
    return c.post(f"/api/projects/{pid}/asset-generations", json=body)


def test_同一意图点两下只排一条作业(as_role, studio):
    """任务书 §11.2「重复点击不会产生重复作业」。

    0033 那一轮 `AssetGenIn` 有 `idempotency_key` 这一格，但**没人传**，
    而 `services.submit_job` 也不查键——`job.idempotency_key` 上那条 UNIQUE
    于是成了个陷阱：手滑双击是真两条计费作业，而老实传同一个键的请求反而撞约束变 500。
    """
    c = as_role("editor")
    before = _queued_count(studio["pid"])
    first = _gen(c, studio["pid"], operation="asset_image", image_prompt="掌灯人 雨夜")
    assert first.status_code == 202, first.text
    assert first.json()["reused"] is False
    second = _gen(c, studio["pid"], operation="asset_image", image_prompt="掌灯人 雨夜")
    assert second.status_code == 202, second.text
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["reused"] is True
    assert "平台没有再排一条" in second.json()["note"]
    assert _queued_count(studio["pid"]) == before + 1          # 只多一条


def test_换一批是人的决定而不是页面的手滑(as_role, studio):
    """内容一字不差，但界面显式带了一个新键——那就真的再排一条。

    这一条存在理由是：**幂等不能变成"同一句话永远只许跑一次"**。
    同一个种子换一批图是合理需求，代价是它必须由人点出来（界面上那颗「换一批」），
    不能由页面在他没要求的时候偷偷做。
    """
    c = as_role("editor")
    before = _queued_count(studio["pid"])
    first = _gen(c, studio["pid"], operation="asset_image", image_prompt="同一句话")
    again = _gen(c, studio["pid"], operation="asset_image", image_prompt="同一句话",
                 idempotency_key="asset-again-i-abc123")
    assert again.status_code == 202, again.text
    assert again.json()["job_id"] != first.json()["job_id"]
    assert _queued_count(studio["pid"]) == before + 2


def test_提示词一改就是新意图(as_role, studio):
    c = as_role("editor")
    a = _gen(c, studio["pid"], operation="asset_image", image_prompt="第一句")
    b = _gen(c, studio["pid"], operation="asset_image", image_prompt="第二句")
    assert a.json()["job_id"] != b.json()["job_id"]


def test_补全缺失只请求空着的那几段(as_role, studio):
    """`fill_missing` 的全部意义就在这：已有的一个字不动。"""
    c = as_role("editor")
    c.put(f"/api/assets/{studio['aid']}/character-profile",
          json={"biography": "已经有履历了", "personality": "", "growth_arc": ""})
    r = _gen(c, studio["pid"], operation="character_profile",
             target_asset_id=studio["aid"], text_mode="fill_missing")
    assert r.status_code == 202, r.text
    refs = json.loads(job_repo.get(r.json()["job_id"])["input_refs_json"])
    assert "mode:fill_missing" in refs
    assert "sec:biography" not in refs                    # 有内容的不必再要
    assert "sec:personality" in refs and "sec:growth_arc" in refs
    prompt = job_repo.get(r.json()["job_id"])["prompt_snapshot"]
    assert "只写这些" in prompt and "【履历】" not in prompt


def test_扩写只出那一段且段名要对得上(as_role, studio):
    c = as_role("editor")
    r = _gen(c, studio["pid"], operation="character_profile", target_asset_id=studio["aid"],
             text_mode="expand", target_field="personality")
    assert r.status_code == 202, r.text
    refs = json.loads(job_repo.get(r.json()["job_id"])["input_refs_json"])
    assert [x for x in refs if x.startswith("sec:")] == ["sec:personality"]

    bad = _gen(c, studio["pid"], operation="character_profile",
               target_asset_id=studio["aid"], text_mode="expand", target_field="血型")
    assert bad.status_code == 422 and "能扩写的是" in bad.json()["detail"]


def test_依据原文必须真给原文(as_role, studio):
    c = as_role("editor")
    empty = _gen(c, studio["pid"], operation="character_profile",
                 target_asset_id=studio["aid"], text_mode="from_source")
    assert empty.status_code == 422 and "要先给一段原文" in empty.json()["detail"]

    r = _gen(c, studio["pid"], operation="character_profile", target_asset_id=studio["aid"],
             text_mode="from_source", source_excerpt="她提着一盏灯站在渡口。")
    assert r.status_code == 202, r.text
    prompt = job_repo.get(r.json()["job_id"])["prompt_snapshot"]
    assert "她提着一盏灯站在渡口。" in prompt and "只依据" in prompt


def test_未知任务模式回人话并列出可选(as_role, studio):
    c = as_role("editor")
    r = _gen(c, studio["pid"], operation="character_profile",
             target_asset_id=studio["aid"], text_mode="magic")
    assert r.status_code == 422
    assert "生成完整设定" in r.json()["detail"]      # 告诉人有哪几种，而不是只说不认识


def test_回稿按当时请求的段落切(as_role, studio):
    """**这一条是模式这件事的收口**：切段落用的是作业里记的 sec，不是现在界面选的模式。

    反例：人跑了 fill_missing 拿到两段，保存进库，再打开这一页时"缺"的格子变了——
    这时候按"当前缺什么"去切同一份回稿，会把已经填上的那段读成没填，
    或者把没请求的段当成请求了的段。
    """
    pid, aid = studio["pid"], studio["aid"]
    job = _make_job(pid, "text",
                    ["op:character_profile", "mode:fill_missing", "sec:personality",
                     f"asset:{aid}"],
                    state="succeeded",
                    files={"result.txt": "【履历】不该被读进来\n【性格】认死理\n".encode()})
    c = as_role("editor")
    r = c.get(f"/api/projects/{pid}/asset-generations/{job['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text_mode"] == "fill_missing"
    assert body["fields"] == {"personality": "认死理"}
    assert "只请求了" in body["note"]


def test_优化已有设定不改事实那句话会进提示词(as_role, studio):
    c = as_role("editor")
    c.put(f"/api/assets/{studio['aid']}/character-profile",
          json={"biography": "已有", "personality": "已有", "growth_arc": "已有",
                "text_prompt": ""})
    r = _gen(c, studio["pid"], operation="character_profile",
             target_asset_id=studio["aid"], text_mode="polish")
    prompt = job_repo.get(r.json()["job_id"])["prompt_snapshot"]
    assert "不新增事实" in prompt


def test_档案带旧令牌保存回409且库里不动(as_role, studio):
    """§12「用户编辑期间资产被其他请求修改」。

    这一条同时钉住"为什么令牌不能就是 updated_at"：两次 PUT 在同一秒里跑完，
    `asset.updated_at` 完全相同，按时间比就一律判不出冲突（第一版正是这样写，
    测试里连同一秒的第二次保存都拦不住）。
    """
    c = as_role("editor")
    url = f"/api/assets/{studio['aid']}/character-profile"
    first = c.put(url, json={"biography": "我先写的一版", "seen_version": ""})
    assert first.status_code == 200, first.text
    seen = first.json()["version"]
    assert seen                                        # 令牌得真的发出来了
    stale = c.put(url, json={"biography": "我手上是旧版", "seen_version": "旧的那把"})
    assert stale.status_code == 409, stale.text
    assert "没有保存" in stale.json()["detail"]
    assert profiles.get(studio["aid"])["biography"] == "我先写的一版"
    # 带对了令牌就放行，并且回一把新的（界面下一次保存要用的就是它）
    ok = c.put(url, json={"biography": "看过了，还是要改", "seen_version": seen})
    assert ok.status_code == 200, ok.text
    assert ok.json()["version"] != seen
    assert profiles.get(studio["aid"])["biography"] == "看过了，还是要改"


def test_资产带旧令牌保存也回409(as_role, studio):
    c = as_role("editor")
    def put(name, seen=None):
        return c.put(f"/api/assets/{studio['aid']}", json={
            "kind": "character", "name": name, "summary": "",
            "consistency_prompt": "", "aliases": [], "tags": [],
            "scope": "project", **({"seen_version": seen} if seen is not None else {})})
    got = c.get(f"/api/assets/{studio['aid']}").json()
    assert put("先改的人").status_code == 200
    r = put("后到的手", got["version"])
    assert r.status_code == 409, r.text
    assert asset_repo.get(studio["aid"])["name"] == "先改的人"
    # 不带令牌的写法照旧放行：这道闸是给编辑界面用的，不是给脚本设的路障
    assert put("脚本改的名").status_code == 200


def test_形态的图可以单独摘掉且文件跟着删(as_role, studio):
    """0033 有 `remove_image` 却没有接口：导错一张只能删整个形态。"""
    job, _ = _image_job(studio["pid"])
    aid = _new_asset(studio["pid"])
    vid = variants.create(asset_id=aid, variant_type="outfit", name="常服")["id"]
    c = as_role("editor")
    first = c.post(f"/api/assets/{aid}/generated-images",
                   json={"job_id": job["id"], "artifact_index": 0, "variant_id": vid})
    again = c.post(f"/api/assets/{aid}/generated-images",
                   json={"job_id": job["id"], "artifact_index": 0, "variant_id": vid})
    paths = again.json()["ref_images"]
    assert len(paths) == 2 and paths[0] != paths[1]      # 追加，不覆盖

    gone = paths[1]
    r = c.delete(f"/api/asset-variants/{vid}/images?path={gone}")
    assert r.status_code == 200, r.text
    assert r.json()["image_count"] == 1
    assert r.json()["ref_images"] == [paths[0]]
    assert not (FILES_DIR / gone).exists()               # 盘上那份也跟着清了
    assert variants.get(vid) is not None                 # 形态本身还在

    # 另一张同名路径再删一次：不在清单里就是 404，不能回"删好了"
    assert c.delete(f"/api/asset-variants/{vid}/images?path={gone}").status_code == 404


def test_摘形态图只认这个形态自己名下的图(as_role, studio):
    job, _ = _image_job(studio["pid"])
    aid = _new_asset(studio["pid"])
    v1 = variants.create(asset_id=aid, variant_type="outfit", name="常服")["id"]
    v2 = variants.create(asset_id=aid, variant_type="outfit", name="雨夜外套")["id"]
    c = as_role("editor")
    path = c.post(f"/api/assets/{aid}/generated-images",
                  json={"job_id": job["id"], "artifact_index": 0, "variant_id": v1}
                  ).json()["path"]
    # 拿 v1 的图去删 v2：路径不在 v2 的清单里 → 404，且 v1 那张与文件都不许动
    assert c.delete(f"/api/asset-variants/{v2}/images?path={path}").status_code == 404
    assert json.loads(variants.get(v1)["ref_images"]) == [path]
    assert (FILES_DIR / path).is_file()


def test_清单说得出这一档能不能吃参考图():
    """§6.3「不支持参考图的模型不能被宣称能保持人物一致性」的数据出处。

    判据是**登记的参数类型**，不是模型名：`type=="image"` 的参数存在才算支持。
    mflux（本地图像档）没有这样的参数，所以界面上那句"不会把基础图喂进去"
    是清单说的，不是页面猜的。
    """
    import adapters

    i2v = adapters.find("video", "local", "mlx-video")
    t2v = next(m for m in i2v.models if m.code == "wan2.2-t2v-a14b")
    i2v_model = next(m for m in i2v.models if m.code == "wan2.2-i2v-a14b")
    assert i2v_model.as_dict()["image_inputs"] == ["image"]
    assert t2v.as_dict()["image_inputs"] == []

    mflux = adapters.find("image", "local", "mflux")
    assert all(m.as_dict()["image_inputs"] == [] for m in mflux.models)


# ---------- 取数窗口与脏数据（2026-11-04 优化轮补）----------

def test_同项目作业再多也看得见刚生成的那几条(studio):
    """`recent_generations` 的窗口以前是"取最近 48 条任意 kind 再筛"。

    同项目里视频与文档 AI 作业一多，要的资产生成作业就整个落在窗口外面，
    界面显示"还没有生成记录"——**假空态**：数据在库里，只是没被读出来。
    现在过滤下推到 SQL（refs 以 `["op:` 开头的那些行），所以后面压多少条
    别的 kind 都不影响这一页读到自己的历史。
    """
    pid, aid = studio["pid"], studio["aid"]
    # 先放 3 条真的资产生成作业（文本，refs 由 build_refs 生成）
    for _ in range(3):
        _make_job(pid, "text", asset_studio.build_refs("character_profile", aid, None))
    # 再压 60 条**不是资产生成**的作业（成片与产线那条路存的是路径数组，
    # docai 存的是对象数组）——它们时间上都更新，旧写法会把上面 3 条挤出窗口
    for _ in range(60):
        _make_job(pid, "image", ["data/jobs/1/outputs/frame.png"])

    out = asset_studio.recent_generations(pid, asset_id=aid, limit=12)
    assert len(out) == 3, f"该读到自己的 3 条，实得 {len(out)} 条"
    assert all(o["target_asset_id"] == aid for o in out), out

    # 按别的资产筛仍然是空的：下推过滤只负责"是资产生成作业"，归属仍逐条判
    assert asset_studio.recent_generations(pid, asset_id=999999, limit=12) == []


def test_脏的图片清单不会把删除卡成半套(as_role, fresh_project, tmp_path, delete_guard):
    """删资产时那份清单的解析必须走已有出处 `_images_of`。

    原来路由里手写 `json.loads` + 一段 unlink 循环：`ref_images` 里混进非字符串
    （或整列是坏 JSON）就会在"行已删、文件清一半"的中间态抛出来，
    而这条资产之后再也删不掉。现在坏数据当空清单，越出目录的路径仍被
    `resolve_within` 挡在外面——两件事都不能退。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    a = _new_asset(pid)
    good = FILES_DIR / str(a) / "ok.png"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(PNG)
    outside = tmp_path / "外面的图.png"
    outside.write_bytes(PNG)
    with get_conn() as conn:
        conn.execute("UPDATE asset SET ref_images = ? WHERE id = ?",
                     (json.dumps([str(good.relative_to(FILES_DIR)), "../逃逸.png",
                                  123, None, str(outside), "9999/不存在.png"]), a))

    assert c.request("DELETE", f"/api/assets/{a}").status_code == 204
    assert not good.exists(), "在本目录里的那张该跟着资产走"
    assert outside.exists(), "越出 FILES_DIR 的路径不是本平台资产，动不得"
    assert not (FILES_DIR / "逃逸.png").exists(), "上跳一层不能把文件落进上传目录"
