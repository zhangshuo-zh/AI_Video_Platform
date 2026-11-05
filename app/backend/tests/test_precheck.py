"""统一预检（任务书 §7 与 §9.1）。

这一层最该被钉住的不是"清单好不好看"，而是一句话：
**预检说阻塞的，提交一定失败；预检说通过的，提交不该因为这几类原因失败。**
否则预检就只是一个更贵、还更爱骗人的弹窗。

所以本文件有三类用例：
1. 单项规则能不能被数据触发（内容、资产、模型、字幕、镜头完整性）；
2. 三档汇总的口径（阻塞优先于警告，警告优先于通过）；
3. 预检与真实提交/合成的一致性——拿同一条数据两边各跑一次。
"""
import json

import precheck
import services
from repositories.content import shots

# ---------------------------------------------------------------- 建数据


def _project_with_episode(client):
    pid = client.post("/api/projects", json={"name": "预检项目", "description": "",
                                             "tags": []}).json()["id"]
    eid = client.post(f"/api/projects/{pid}/episodes",
                      json={"title": "第 1 集"}).json()["id"]
    return pid, eid


def _shot(client, eid, summary="一镜", prompt="提示词", asset_ids=None):
    return client.post(f"/api/episodes/{eid}/shots", json={
        "summary": summary, "prompt": prompt, "duration": 5,
        "asset_ids": asset_ids or []}).json()


def _codes(result: dict) -> dict[str, str]:
    return {i["code"]: i["level"] for i in result["items"]}


# ---------------------------------------------------------------- 内容 / 资产


def test_旧产物的源版本变了要在生成前说清(as_role, fresh_project, clean_queue):
    """预检第⑦项（场景三真跑时补上的那条）。

    判据不自己写：与时间线取片同一个 `services.job_source_stale`。
    这一项是警告不是阻塞——重新生成本来就是"源变了"的解法，
    判成阻塞会让人连重做都点不动；但不写就等于让人以为旧视频还能进成片。
    """
    import jobfiles
    from repositories.jobs import jobs as job_repo

    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    shot = _shot(c, eid, summary="按这一版生成", prompt="提示词")
    sid = shot["id"]

    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "fastmetal", "model_code": "fastmetal-1.3b-qad",
        "adapter_version": "test", "prompt_snapshot": "提示词", "params": {},
        "input_refs": [], "source_version": services.shot_source_version(shots.get(sid))})
    assert job_repo.claim_next("pre-worker", 60)["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"fake-video-bytes")
    job_repo.succeed(row["id"], "pre-worker", "", [jobfiles.relative(target)], 1)

    # 同版本时不该无病呻吟
    assert "artifact.source_stale" not in _codes(precheck.shot_precheck(sid))

    c.put(f"/api/shots/{sid}", json={
        "summary": "改了内容", "prompt": "提示词", "duration": 5,
        "asset_ids": []})
    codes = _codes(precheck.shot_precheck(sid))
    assert codes.get("artifact.source_stale") == precheck.WARN, codes
    # 这一项自己不是阻塞：整份清单的结论仍由"有没有默认轨、内容全不全"那些项决定，
    # 临时库里没配默认轨本来就是阻塞，不该把两件事混在一条断言里。
    assert precheck.WARN in codes.values()


def test_没有文字就没有可交给模型的东西(as_role, fresh_project):
    """summary 与 prompt 同时为空＝阻塞；只有 prompt 空＝警告。

    这两档必须分开：平台确实会拿画面描述代替提示词，那是警告；
    而两栏都空时子进程拿到的是空串，跑完只会得到一条失败作业。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = _shot(c, eid, summary="只有画面描述", prompt="").pop("id")
    assert _codes(precheck.shot_precheck(sid))["content.prompt"] == precheck.WARN

    # 两栏都空要绕过接口直接建行：PUT 的 min_length=1 会先拦下来，
    # 而线上确实可能有历史脏行——预检必须对这种行也说真话。
    row = shots.create(eid, "", "", 5, [])
    codes = _codes(precheck.shot_precheck(int(row["id"])))
    assert codes.get("content.empty") == precheck.BLOCK


def test_引用不存在的资产是阻塞而不是警告(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    # 走仓储建一条坏引用行：接口那层会把不存在的资产 id 拒掉，
    # 但库里可能躺着历史脏数据（资产被删过），预检要能点名。
    row = shots.create(eid, "一镜", "提示词", 5, [999999])
    codes = _codes(precheck.shot_precheck(int(row["id"])))
    assert codes.get("asset.missing") == precheck.BLOCK


def test_缺参考图只警告不拦人(as_role, fresh_project):
    """一致性提示词与参考图是"画得像不像"的依据，不是"能不能跑"的条件。

    拦下来会让人以为平台不支持无参考图生成，那是把偏好写成禁令。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    aid = c.post(f"/api/projects/{pid}/assets", json={
        "kind": "character", "name": "阿昭", "summary": "", "aliases": [],
        "tags": [], "scope": "project"}).json()["id"]
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = _shot(c, eid, asset_ids=[aid]).pop("id")
    codes = _codes(precheck.shot_precheck(sid))
    assert codes.get("asset.ref_image") == precheck.WARN
    assert codes.get("asset.none") is None       # 有引用，就不该再说"没引用任何资产"


# ---------------------------------------------------------------- 模型 / 路由


def test_没有默认轨时预检直接点名模型那一栏(as_role, fresh_project):
    """今天这台机器 video·本地 那一行还是空的（没填 provider）。

    预检不许把它翻译成"提示词有问题"——那是把配置问题推给内容。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = _shot(c, eid).pop("id")
    result = precheck.shot_precheck(sid)
    codes = _codes(result)
    assert codes.get("model.route") == precheck.BLOCK
    bad = next(i for i in result["items"] if i["code"] == "model.route")
    assert bad["locate"]["module"] == "models", "要能跳到引擎管理，不是只说一句"


def test_云端没确认计费是阻塞(as_role, fresh_project, provider_state):
    """`resolve_provider` 自己就拦云端视频，预检必须给出同一句结论而不是另一句。

    这里把 视频·云端 那一行配起来（`provider_state` 用完还原），
    让路由能解析成功，只剩"计费确认"这一道——否则测的就不是这一关了。
    """
    from repositories.providers import providers

    c = as_role("editor")
    pid = fresh_project("editor")
    eid = c.post(f"/api/projects/{pid}/episodes", json={"title": "第 1 集"}).json()["id"]
    sid = _shot(c, eid).pop("id")

    rows = [{"id": r["id"], "provider": r["provider"], "model": r["model"],
             "enabled": r["enabled"], "is_default": r["is_default"],
             "key_env": r["key_env"], "notes": r["notes"]} for r in providers.all()]
    for row in rows:
        saved = next(r for r in providers.all() if r["id"] == row["id"])
        if saved["capability"] == "video" and saved["backend"] == "cloud":
            row.update({"provider": "wan", "model": "wan2.7-t2v", "enabled": 1})
    providers.replace_many(rows)

    result = precheck.shot_precheck(sid, capability="video",
                                    execution_mode="cloud",
                                    confirmed_cloud=False)
    # 云端视频这一关由 `resolve_provider()` 拦（NoRoute 里就写着"要逐镜头勾选并确认计费"），
    # 预检把它原样搬过来，不自己再编一句——所以断言看的是"有没有点名计费"，
    # 而不是某个具体 code 名。
    assert result["status"] == precheck.BLOCK
    assert any("计费" in i["text"] for i in result["items"]), result["items"]

    again = precheck.shot_precheck(sid, capability="video",
                                   execution_mode="cloud", confirmed_cloud=True)
    assert _codes(again).get("cost.cloud") == precheck.WARN


# ---------------------------------------------------------------- 三档汇总


def test_阻塞优先于警告优先于通过():
    items = [precheck._item("a", precheck.WARN, "警告一项"),
             precheck._item("b", precheck.BLOCK, "阻塞一项"),
             precheck._item("c", precheck.PASS, "通过一项")]
    out = precheck._rollup(items)
    assert out["status"] == precheck.BLOCK
    assert (out["total"], out["blocking"], out["warnings"], out["passed"]) == (3, 1, 1, 1)
    assert precheck._rollup(items[:1] + items[2:])["status"] == precheck.WARN
    assert precheck._rollup([items[2]])["status"] == precheck.PASS


def test_空集的合成预检说清缺什么(as_role, fresh_project):
    c = as_role("editor")
    pid, eid = _project_with_episode(c)
    out = precheck.composition_precheck(pid, eid)
    codes = _codes(out)
    assert codes.get("shots.none") == precheck.BLOCK
    assert out["status"] == precheck.BLOCK


def test_有镜头没片时时间线那条要点名(as_role, fresh_project):
    c = as_role("editor")
    pid, eid = _project_with_episode(c)
    _shot(c, eid)
    out = precheck.composition_precheck(pid, eid)
    blocking = [i["text"] for i in out["items"] if i["level"] == precheck.BLOCK]
    assert any("视频" in t or "签字" in t or "可用" in t for t in blocking), blocking


def test_阻塞项能定位到具体那一镜(as_role, fresh_project):
    """§7.2 要的"点击问题定位到具体镜头"。定位错等于没定位，所以断言到 id。"""
    c = as_role("editor")
    pid, eid = _project_with_episode(c)
    pid = pid or fresh_project("editor")
    sids = [c.post(f"/api/episodes/{eid}/shots", json={
        "summary": f"第 {i} 镜", "prompt": "提示词", "duration": 5,
        "asset_ids": []}).json()["id"] for i in (1, 2)]
    out = precheck.composition_precheck(pid, eid)
    blocking = [i for i in out["items"] if i["level"] == precheck.BLOCK
                and i["code"] == "timeline.blocking"]
    assert len(blocking) == 2, out["items"]
    located = {i["locate"].get("shot_id") for i in blocking}
    assert located == set(sids), located
    assert all(i["locate"]["module"] == "shot" for i in blocking)


def test_单镜预检每条都带这一镜的定位(as_role, fresh_project):
    """整张清单都是这一镜的：每条都该能跳回这一镜，而不是丢回列表页再找。"""
    c = as_role("editor")
    pid, eid = _project_with_episode(c)
    sid = c.post(f"/api/episodes/{eid}/shots", json={
        "summary": "只有一句描述", "prompt": "", "duration": 5,
        "asset_ids": []}).json()["id"]
    out = precheck.shot_precheck(sid)
    assert out["items"], out
    # 每条都带上「是哪一镜」；但**该去哪儿修仍按条目自己的指向**——
    # 提示词空了要去分镜页改，不是把工作台甩给人让他自己找输入框
    assert all(i["locate"].get("shot_id") == sid for i in out["items"]), out["items"]
    codes = _codes(out)
    assert codes.get("content.prompt") == precheck.WARN
    content = next(i for i in out["items"] if i["code"] == "content.prompt")
    assert content["locate"]["module"] == "writing" and content["locate"]["sub"] == 2
    model = next(i for i in out["items"] if i["code"].startswith("model"))
    assert model["locate"]["module"] == "models" or model["locate"]["module"] == "shot"


def test_字幕不在本项目目录里就是阻塞(as_role, fresh_project):
    c = as_role("editor")
    pid, eid = _project_with_episode(c)
    _shot(c, eid)
    out = precheck.composition_precheck(pid, eid, subtitle_path="../999/x.srt")
    assert _codes(out).get("subtitle.bad") == precheck.BLOCK


# ---------------------------------------------------------------- 接口与一致性


def test_三个预检接口都挂在项目可见性后面(as_role, fresh_project, people):
    c = as_role("editor")
    pid, eid = _project_with_episode(c)
    sid = _shot(c, eid).pop("id")
    assert c.get(f"/api/precheck/shot/{sid}").status_code == 200
    assert c.get(f"/api/projects/{pid}/precheck?episode_id={eid}").status_code == 200
    assert c.get(f"/api/projects/{pid}/composition-precheck?episode_id={eid}"
                 ).status_code == 200
    outsider = as_role("outsider")
    assert outsider.get(f"/api/precheck/shot/{sid}").status_code == 404
    assert outsider.get(f"/api/projects/{pid}/precheck?episode_id={eid}"
                        ).status_code == 404


def test_预检阻塞的那条提交也一定失败(as_role, fresh_project):
    """这一条是本文件的正菜：预检不许是个爱骗人的弹窗。

    默认轨没配 → 预检阻塞 → 提交必须同样被拒（422/409），
    两边说的是同一件事，而不是一个放行一个拒绝。
    """
    c = as_role("editor")
    pid, eid = _project_with_episode(c)
    sid = _shot(c, eid).pop("id")
    result = c.get(f"/api/precheck/shot/{sid}").json()
    assert result["status"] == precheck.BLOCK
    r = c.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "prompt": "提示词", "params": {}})
    assert r.status_code in (409, 422), r.text
