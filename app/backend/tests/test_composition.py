"""成片中心（阶段 D）：真 FFmpeg 合成、前检查、版本不覆盖。

这些用例真的起 ffmpeg/ffprobe（本机已装 9.x），用 lavfi 生成两段纯色片段，
再走真实的 composition service + worker + Job 链路。不是 mock 合成：
产出的 mp4 用 ffprobe 读得出来，才说明"能生成一个真实可播放的视频文件"。
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

import jobfiles
import services
import worker
from composition import service as composition_service
from composition import validators
from config import FFMPEG_BIN
from repositories.audio import clips as audio_repo
from repositories.content import episodes, shots
from repositories.jobs import jobs as job_repo
from db import get_conn
from repositories.reviews import reviews

WID = "compose-test-worker"


def _has_ffmpeg() -> bool:
    try:
        subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg")


def _clip(project_id: int, episode_id: int, shot_id: int,
          user_id: int, color: str, seconds: float = 1.0) -> dict:
    """造一条"已成功"的视频作业：真文件 + 真产物清单。"""
    job = job_repo.create({
        "project_id": project_id, "episode_id": episode_id, "shot_id": shot_id,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": f"片段 {color}", "params": {}, "input_refs": [],
        "created_by": user_id,
    })
    claimed = job_repo.claim_next(WID, 60)
    assert claimed is not None and claimed["id"] == job["id"]
    _, outputs = jobfiles.ensure_dirs(job["id"])
    target = outputs / f"{job['id']}-{color}.mp4"
    subprocess.run([
        FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s=640x360:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24", str(target),
    ], check=True, capture_output=True)
    job_repo.succeed(job["id"], WID, "", [jobfiles.relative(target)], 10)
    return job_repo.get(job["id"])


def _approve(job, project_id: int, user_id: int) -> None:
    reviews.upsert_sign(
        project_id=project_id, target_kind=reviews.JOB, target_id=job["id"],
        gate_id=None, base_version=services.artifact_version(job),
        anchor="artifact", decision="approved", note="测试通过", user_id=user_id)


@pytest.fixture
def stage(fresh_project, people, clean_queue):
    project_id = fresh_project("editor")
    eid = episodes.create(project_id, f"成片测试集_{uuid.uuid4().hex[:5]}")["id"]
    s1 = shots.create(eid, "第一镜", "", 1.0, [])["id"]
    s2 = shots.create(eid, "第二镜", "", 1.0, [])["id"]
    job1 = _clip(project_id, eid, s1, people["editor"]["id"], "red")
    job2 = _clip(project_id, eid, s2, people["editor"]["id"], "blue")
    return {"project_id": project_id, "episode_id": eid,
            "shot_ids": [s1, s2], "jobs": [job1, job2],
            "user_id": people["editor"]["id"]}


def test_两镜合成出真实可播放文件(stage):
    for job in stage["jobs"]:
        _approve(job, stage["project_id"], stage["user_id"])
    comp = composition_service.create(
        stage["project_id"], stage["episode_id"], stage["user_id"],
        params={"width": 640, "height": 360, "fps": 24})
    assert comp["status"] == "queued"
    assert comp["shot_count"] == 2
    assert comp["job_id"]

    ran = worker.run_once(WID)
    assert ran == comp["job_id"]
    view = composition_service.view(comp["id"])
    assert view["status"] == "succeeded", view["error_message"]
    assert view["sha256"] and len(view["sha256"]) == 64
    assert view["bytes"] > 0 and view["duration_ms"] >= 1500
    assert (view["width"], view["height"]) == (640, 360)
    path = jobfiles.resolve_artifact(view["output_path"])
    assert path is not None
    info = validators.inspect(path)
    assert info["video_codec"] == "h264"
    assert info["width"] == 640 and info["height"] == 360
    # 时间线冻结：镜头顺序与产物引用都在
    timeline = view["timeline"]
    assert [s["seq"] for s in timeline["shots"]] == [1, 2]
    assert [s["video"]["job_id"] for s in timeline["shots"]] == [
        stage["jobs"][0]["id"], stage["jobs"][1]["id"]]


def test_未审核的镜头会被前置检查拒绝(fresh_project, people, clean_queue):
    project_id = fresh_project("editor")
    eid = episodes.create(project_id, f"未审集_{uuid.uuid4().hex[:5]}")["id"]
    sid = shots.create(eid, "唯一镜", "", 1.0, [])["id"]
    _clip(project_id, eid, sid, people["editor"]["id"], "green")
    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(project_id, eid, people["editor"]["id"])
    assert any("审核" in r for r in exc.value.reasons)
    assert "未审核" in "".join(exc.value.reasons) or "签字" in "".join(exc.value.reasons)


def test_每次合成版本加一且旧版本不覆盖(stage):
    for job in stage["jobs"]:
        _approve(job, stage["project_id"], stage["user_id"])
    first = composition_service.create(stage["project_id"], stage["episode_id"],
                                       stage["user_id"],
                                       params={"width": 640, "height": 360})
    worker.run_once(WID)
    second = composition_service.create(stage["project_id"], stage["episode_id"],
                                        stage["user_id"],
                                        params={"width": 640, "height": 360})
    assert second["version"] == first["version"] + 1
    old = composition_service.view(first["id"])
    assert old["version"] == 1
    assert old["output_path"]        # 旧版本仍在，没有被覆盖
    # 同一集上版本号唯一：直接塞同版会撞 UNIQUE，而不是静默改旧行
    assert json.loads(comp_row(second["id"])["timeline_json"])["shots"]


def comp_row(composition_id: int):
    from repositories.compositions import compositions
    return compositions.get(composition_id)


def test_缺片会一次说清是哪几镜(fresh_project, people, clean_queue):
    project_id = fresh_project("editor")
    eid = episodes.create(project_id, f"缺片集_{uuid.uuid4().hex[:5]}")["id"]
    shots.create(eid, "有片", "", 1.0, [])
    shots.create(eid, "没片", "", 1.0, [])
    first = shots.list_by_episode(eid)[0]
    _clip(project_id, eid, first["id"], people["editor"]["id"], "yellow")
    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(project_id, eid, people["editor"]["id"])
    text = "；".join(exc.value.reasons)
    assert "第 1 镜" in text and "第 2 镜" in text


def test_镜头与音频的问题同一次说完(fresh_project, people, clean_queue):
    """缺片与"音频文件不在了"是同一次发起的两半，不许分两轮说。

    原来时间线先抛镜头的问题，音频那条检查根本走不到：改完镜头再点发起，
    才发现音频也没了——一次发起该把所有已经确定的原因一次摊开。
    """
    project_id = fresh_project("editor")
    eid = episodes.create(project_id, f"混集_{uuid.uuid4().hex[:5]}")["id"]
    sid = shots.create(eid, "唯一镜", "", 1.0, [])["id"]   # 故意不给它任何视频作业
    clip = audio_repo.create(
        project_id=project_id, episode_id=eid, shot_id=sid, kind="dialogue",
        speaker="燧人", text="火。", created_by=people["editor"]["id"])
    audio_repo.finish(clip["id"], status="ready", path="999999/outputs/gone.wav",
                      duration_ms=800)

    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(project_id, eid, people["editor"]["id"])
    text = "；".join(exc.value.reasons)
    assert "第 1 镜没有成功的视频作业" in text, text
    assert "音频素材" in text and "文件不在" in text, text


def test_音频按镜头起点混入成片(stage):
    """音频不是"贴上去就行"：镜头 2 的台词要从镜头 2 的起点开始，
    所以 offset 必须按实际时长累计算；成片验收还要确认音轨真的存在。"""
    for job in stage["jobs"]:
        _approve(job, stage["project_id"], stage["user_id"])
    # 台词挂在第二镜：它的起点应该＝第一镜的实际时长
    first_clip = stage["jobs"][0]
    _, outputs = jobfiles.ensure_dirs(first_clip["id"])
    voice = outputs / "voice.wav"
    subprocess.run([
        FFMPEG_BIN, "-hide_banner", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=0.8", "-ac", "1", str(voice),
    ], check=True, capture_output=True)
    rel = jobfiles.relative(voice)
    clip = audio_repo.create(
        project_id=stage["project_id"], episode_id=stage["episode_id"],
        shot_id=stage["shot_ids"][1], kind="dialogue", speaker="燧人",
        text="火。", created_by=stage["user_id"])
    audio_repo.finish(clip["id"], status="ready", path=rel, duration_ms=800)

    comp = composition_service.create(
        stage["project_id"], stage["episode_id"], stage["user_id"],
        params={"width": 640, "height": 360})
    assert comp["audio_count"] == 1
    assert comp["audio"][0]["start_ms"] >= 900      # 第一镜 1s 之后
    worker.run_once(WID)
    view = composition_service.view(comp["id"])
    assert view["status"] == "succeeded", view["error_message"]
    path = jobfiles.resolve_artifact(view["output_path"])
    info = validators.inspect(path)
    assert info["has_audio"]
    assert "aac" in info["audio_codecs"]


# ---------- 字幕（后端有滤镜才谈得上烧） ----------

SRT = ("1\n00:00:00,200 --> 00:00:01,800\n火\n\n"
       "2\n00:00:01,900 --> 00:00:02,400\n取火\n")


def _subtitle(project_id: int, text: str = SRT) -> str:
    return jobfiles.save_subtitle(project_id, text.encode("utf-8"), ".srt")


def _signed(stage):
    for job in stage["jobs"]:
        _approve(job, stage["project_id"], stage["user_id"])
    return stage


def _compose(stage, **kw):
    _signed(stage)
    comp = composition_service.create(
        stage["project_id"], stage["episode_id"], stage["user_id"],
        params={"width": 640, "height": 360, "fps": 24}, **kw)
    worker.run_once(WID)
    return composition_service.view(comp["id"])


# 字幕带在画面里的位置与占比：SRT 默认落在下方，取底部 30% 为"该被动"的区域，
# 顶部 50% 为"不该被动"的对照区。
BAND_BOTTOM = "ih*0.3:0:ih*0.7"
BAND_TOP = "ih*0.5:0:0"


def _psnr(a: Path, b: Path, band: str = "") -> float:
    """两份成片的平均 PSNR（dB）。`band` 给几何串就只比那一条带。

    原来这里只比整幅平均值，并断言 `< 40`——CI（ubuntu 的 ffmpeg 带 libass）上
    稳定红在 42.9：字幕确实烧进去了，但 640×360 里那行字只占画面一小部分，
    整幅平均自然掉不到 40 以下。**这条断言量错了东西**：它把"字幕占多少像素"
    这件事当成了"字幕有没有生效"。本机没有 libass，这一支从来不走，所以
    这个错只在 CI 上暴露——也正是 CI 一直红的原因。
    """
    graph = ("[0:v][1:v]psnr" if not band else
             f"[0:v]crop=iw:{band}[c0];[1:v]crop=iw:{band}[c1];[c0][c1]psnr")
    proc = subprocess.run(
        [FFMPEG_BIN, "-hide_banner", "-i", str(a), "-i", str(b),
         "-filter_complex", graph, "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    text = (proc.stderr or "") + (proc.stdout or "")
    tail = [line for line in text.splitlines() if "average:" in line]
    assert tail, f"psnr 没读出平均：{text[-400:]}"
    raw = tail[-1].split("average:", 1)[1].strip().split(" ")[0]
    return float("inf") if raw == "inf" else float(raw)


def test_字幕按这台机器的能力分档处理(stage):
    """有 libass 就真烧进画面（拿像素差证明），没有就在提交前拒掉。

    不写成"没滤镜就跳过"：那样在绝大多数开发机上这条测试永远不跑，
    而"提交前就拒"恰恰是这台机器上必须成立的行为——两种情况都得有人钉住。
    """
    path = _subtitle(stage["project_id"])
    if validators.subtitle_filter_available():
        with_sub = _compose(stage, subtitle_path=path)
        assert with_sub["status"] == "succeeded", with_sub["error_message"]
        assert with_sub["timeline"]["subtitles"][0]["path"] == path
        plain = _compose(stage)          # 同一集不带字幕的对照
        assert plain["status"] == "succeeded", plain["error_message"]
        sub_path = jobfiles.resolve_artifact(with_sub["output_path"])
        plain_path = jobfiles.resolve_artifact(plain["output_path"])
        bottom = _psnr(sub_path, plain_path, BAND_BOTTOM)
        top = _psnr(sub_path, plain_path, BAND_TOP)
        # 两条断言各钉一件事，都不依赖"字幕占多少像素"这种机器相关的量：
        #   下带必须真的被画上（差到看不出就红）；
        #   上带必须基本没动（要是连上面都变了，说明烧的是整幅覆盖，那也不对）。
        assert bottom < 45, f"字幕带几乎没有变化（下带 PSNR={bottom}）＝字幕没烧进去"
        assert top > bottom + 6, (
            f"上带({top})与下带({bottom})差得太少：字幕要么没落在下方，"
            "要么整幅都被改了")
    else:
        # 这一档直接调 create()（不走 _compose），所以得自己把镜头签掉：
        # 前置检查先验镜头、再验字幕，镜头没签的话报的是"缺签字"，
        # 这条用例想钉的"缺滤镜就在提交前拒"根本轮不到。
        _signed(stage)
        before = job_repo.counts().get("queued", 0)
        with pytest.raises(composition_service.CompositionInvalid) as exc:
            composition_service.create(stage["project_id"], stage["episode_id"],
                                       stage["user_id"], subtitle_path=path)
        assert "subtitles" in exc.value.message
        # 关键的一条：被拒时**没有**排进队列。排下去只会得到一条
        # "No such filter"的失败作业，白占一格并发、白写一屏日志
        assert job_repo.counts().get("queued", 0) == before


def test_字幕文件不见了或不属于本项目就拒绝(stage, fresh_project, people):
    other = fresh_project("editor")
    foreign = _subtitle(other)          # 别的项目的字幕：路径合法但不属于本项目
    # 同上：直接调 create()，镜头签字要自己补，否则先撞"镜头没签"那一档
    _signed(stage)
    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(stage["project_id"], stage["episode_id"],
                                   stage["user_id"], subtitle_path=foreign)
    assert "不属于这个项目" in exc.value.message or "不在了" in exc.value.message

    mine = _subtitle(stage["project_id"])
    jobfiles.resolve_subtitle(stage["project_id"], mine).unlink()
    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(stage["project_id"], stage["episode_id"],
                                   stage["user_id"], subtitle_path=mine)
    assert "字幕" in exc.value.message



def test_产线钉住的作业优先于最新成功(stage):
    """产线签 A，之后手抽又跑出 B：钉住 A 就必须拼 A，手动路径才是"最新的那条"。

    这就是"产线签 A、成片用 B"那条缺口的反证：不传 shot_jobs 时取最新成功，
    传了以后只认钉住的那条。
    """
    _signed(stage)
    shot1, shot2 = stage["shot_ids"]
    job_a, job_b = stage["jobs"]
    # 同一镜再跑一条"更新"的片段（颜色不同，两条都签）
    newer = _clip(stage["project_id"], stage["episode_id"], shot1,
                  stage["user_id"], "green")
    _approve(newer, stage["project_id"], stage["user_id"])

    pinned = composition_service.create(
        stage["project_id"], stage["episode_id"], stage["user_id"],
        params={"width": 640, "height": 360, "fps": 24},
        shot_jobs={shot1: job_a["id"], shot2: job_b["id"]})
    timeline = composition_service.view(pinned["id"])["timeline"]
    assert timeline["shots"][0]["video"]["job_id"] == job_a["id"]

    # 不钉作业（成片中心的手动路径）时仍取最新成功：这是两条路径的差别
    loose = composition_service.create(
        stage["project_id"], stage["episode_id"], stage["user_id"],
        params={"width": 640, "height": 360, "fps": 24})
    loose_timeline = composition_service.view(loose["id"])["timeline"]
    assert loose_timeline["shots"][0]["video"]["job_id"] == newer["id"]


def test_钉住的作业没签或不属这一镜就点名拒绝(stage):
    shot1, shot2 = stage["shot_ids"]
    job_a, job_b = stage["jobs"]
    # 一条都没签：钉的作业不能被"顺手签了别的那条"带过
    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(
            stage["project_id"], stage["episode_id"], stage["user_id"],
            params={"width": 640, "height": 360, "fps": 24},
            shot_jobs={shot1: job_a["id"], shot2: job_b["id"]})
    text = "；".join(exc.value.reasons)
    assert "第 1 镜" in text and "没有审核签字" in text

    # 把第二镜的作业钉到第一镜上：要说"不属于这一镜"，不能静默换片
    _signed(stage)
    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(
            stage["project_id"], stage["episode_id"], stage["user_id"],
            params={"width": 640, "height": 360, "fps": 24},
            shot_jobs={shot1: job_b["id"], shot2: job_b["id"]})
    assert "不属于这一镜" in "；".join(exc.value.reasons)


def test_改分镜后旧素材算过期(as_role, fresh_project, clean_queue, people):
    """作业记下提交时的源指纹；分镜一改，旧素材就不许再拼进成片。

    这是「改完剧本继续用旧片」那条缺口的反证：版本不是靠时间猜的，
    而是拿提交那一刻的指纹和当前源内容比。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    sid = shots.create(eid, "开场", "雨夜石阶", 2.0, [])["id"]
    job = c.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "prompt": "雨夜石阶",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.01},
    }).json()
    assert job["source_version"]      # 接口回显（JobOut 带这一列）
    assert worker.run_once(f"w-src-{job['id']}") == job["id"]
    c.post(f"/api/jobs/{job['id']}/review-sign",
           json={"anchor": "artifact", "decision": "approved"})

    # 改分镜：同一条作业还成功、签字也还对得上它的产物清单，但源内容变了
    c.put(f"/api/shots/{sid}", json={
        "summary": "改成白天石阶", "prompt": "白天石阶", "duration": 2.0,
        "asset_ids": []})
    from services import job_source_stale
    assert job_source_stale(job_repo.get(job["id"])) is True

    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(pid, eid, people["editor"]["id"],
                                   params={"width": 640, "height": 360, "fps": 24})
    assert "源内容已变" in "；".join(exc.value.reasons)


def test_改引用资产也算源内容变了(as_role, fresh_project, clean_queue, people):
    """分镜没动，但引用的角色定妆描述改了：旧视频同样过期。"""
    from repositories.content import assets as asset_repo
    from services import job_source_stale

    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    aid = asset_repo.create(pid, "character", "林默", "少年", "黑发", [])["id"]
    sid = shots.create(eid, "开场", "林默提灯", 2.0, [aid])["id"]
    job = c.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "prompt": "林默提灯",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.01},
    }).json()
    row = job_repo.get(job["id"])
    assert job_source_stale(row) is False

    asset_repo.update(aid, kind="character", name="林默", summary="少年",
                      consistency_prompt="银发、灰斗篷", aliases=[], tags=[],
                      project_id=pid)
    assert job_source_stale(job_repo.get(job["id"])) is True


def test_分镜列表标出源已变(as_role, fresh_project, clean_queue):
    """分镜行要能自己看出"这条素材已过期"，而不是等成片才报错。

    标记由服务端算：前端拿不到分镜源指纹（要读引用资产的版本信息），
    让浏览器自己判断等于把口径复制一份到 JS。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    sid = shots.create(eid, "开场", "雨夜石阶", 2.0, [])["id"]
    job = c.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "prompt": "雨夜石阶",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.01},
    }).json()
    assert worker.run_once(f"w-shot-{job['id']}") == job["id"]

    rows = c.get(f"/api/episodes/{eid}/shots").json()
    assert rows[0]["source_stale"] is False

    c.put(f"/api/shots/{sid}", json={
        "summary": "改成白天石阶", "prompt": "白天石阶", "duration": 2.0,
        "asset_ids": []})
    rows = c.get(f"/api/episodes/{eid}/shots").json()
    assert rows[0]["source_stale"] is True


def test_分镜的标记与成片用同一条视频作业(as_role, people, fresh_project, clean_queue):
    """「源已变」必须说成片会说的那句话——两边选的是同一条作业。

    这一条钉的是修掉的口径分叉：原来分镜看"每镜最新一条成功作业"（图像也算），
    成片看"每镜最新一条成功的**视频**作业"。于是两种错法都会出现：

    - 改完剧本后重抽一张关键帧（图像作业更新、视频作业仍旧）⇒ 分镜不贴标记，
      点发起却被成片拒——"界面说没事"最坑人的一种；
    - 视频是新的、图像是旧的 ⇒ 分镜贴了标记，成片却过得去——虚警。

    现在两边都只认那条视频作业：重抽的关键帧不改变标记，成片也确实会拒。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    sid = shots.create(eid, "开场", "雨夜石阶", 2.0, [])["id"]
    video = c.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "prompt": "雨夜石阶",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.01},
    }).json()
    assert worker.run_once(f"w-align-{video['id']}") == video["id"]

    # 改分镜 ⇒ 那条视频过期
    c.put(f"/api/shots/{sid}", json={
        "summary": "改成白天石阶", "prompt": "白天石阶", "duration": 2.0,
        "asset_ids": []})
    # 再补一条**更新**的图像作业：它不能把视频那一镜的过期状态洗掉
    image = c.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "image", "prompt": "白天石阶",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.01, "text": "一帧"},
    }).json()
    assert worker.run_once(f"w-align-{image['id']}") == image["id"]

    rows = c.get(f"/api/episodes/{eid}/shots").json()
    assert rows[0]["source_stale"] is True

    # 成片要先过"签字"这一关，才会走到"源内容已变"那一步：签上字再看它点名
    sign = c.post(f"/api/jobs/{video['id']}/review-sign",
                  json={"anchor": "artifact", "decision": "approved"})
    assert sign.status_code == 200, sign.text
    with pytest.raises(composition_service.CompositionInvalid) as exc:
        composition_service.create(pid, eid, people["editor"]["id"],
                                   params={"width": 640, "height": 360, "fps": 24})
    assert "源内容已变" in "；".join(exc.value.reasons)


def test_重试不许把旧素材洗成新素材(as_role, fresh_project, clean_queue):
    """`jobs.retry()` 复制的是**旧提示词快照**，源指纹必须跟着复制。

    少带这一列的后果不是"缺个字段"，而是"过期被洗白"：新作业的
    `source_version` 是空串，而空串按口径＝"旧作业，不判过期"——于是改完剧本
    再去重试那条失败的视频作业，跑出来的新产物既不被分镜标「源已变」，
    也能过成片的前置检查。一条用旧素材渲染的片子就这样进了成片。

    重试的语义本来就是"同一枪再打一次"（提示词、参数、模型全照旧），
    所以正确的做法是继承原作业的源指纹，而不是按当前分镜重算一个。
    """
    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    sid = shots.create(eid, "开场", "雨夜石阶", 2.0, [])["id"]
    first = c.post("/api/jobs", json={
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "prompt": "雨夜石阶",
        "provider_code": "mock", "model_code": "mock-1",
        "execution_mode": "local", "params": {"sleep": 0.01},
    }).json()
    assert first["source_version"]

    # 先领走再让租约过期 ⇒ 收成 failed(worker_lost)，这条路能重试
    claimed = job_repo.claim_next("w-dead-retry", 60)
    assert claimed is not None and claimed["id"] == first["id"]
    with get_conn() as conn:
        conn.execute("""UPDATE job SET lease_until = datetime('now','localtime','-1 seconds')
                        WHERE id = ?""", (first["id"],))
    assert job_repo.recover_stale()["failed"] >= 1

    # 改分镜：重试出来的那条仍然渲染旧提示词，所以它照样是过期素材
    c.put(f"/api/shots/{sid}", json={
        "summary": "改成白天石阶", "prompt": "白天石阶", "duration": 2.0,
        "asset_ids": []})

    again = c.post(f"/api/jobs/{first['id']}/retry")
    assert again.status_code == 202, again.text
    new = again.json()
    assert new["source_version"] == first["source_version"]
    # 复制的是旧提示词快照（JobOut 不回这一列，回库看）——渲染依据没变，
    # 所以源指纹也必须跟着走，不能按当前分镜重算一个"新"的
    assert job_repo.get(new["id"])["prompt_snapshot"] == "雨夜石阶"
    assert services.job_source_stale(job_repo.get(new["id"])) is True


# ---------------------------------------------------------------- 输出尺寸默认档

def test_不填分辨率时成片与素材同尺寸(stage):
    """默认不再放大：合成器原来写死 1280×720，把 640×360 的素材拉大一倍再存。

    本机整集连跑量到过这件事的代价——六条 640×352 的素材拼出 1280×720 的成片、
    5.6 MB；画质不多一个像素，字节与耗时倒是实涨。所以不填时跟着素材走。
    """
    for job in stage["jobs"]:
        _approve(job, stage["project_id"], stage["user_id"])
    view = composition_service.create(stage["project_id"], stage["episode_id"],
                                      stage["user_id"])
    worker.run_once(WID)
    out = composition_service.view(view["id"])
    assert (out["width"], out["height"]) == (640, 360), out["params"]
    assert "与素材同尺寸" in out["params"]["size_basis"], out["params"]


def test_显式填的分辨率仍然优先(stage):
    for job in stage["jobs"]:
        _approve(job, stage["project_id"], stage["user_id"])
    view = composition_service.create(stage["project_id"], stage["episode_id"],
                                      stage["user_id"],
                                      params={"width": 1280, "height": 720})
    worker.run_once(WID)
    out = composition_service.view(view["id"])
    assert (out["width"], out["height"]) == (1280, 720)
    assert out["params"]["size_basis"] == "人填的"


def test_素材尺寸不合规矩时退回默认档并说清():
    """240 高低于范围下限（320）：不许硬用，退回配置档并把原因写在同一栏里。"""
    from config import COMPOSE_HEIGHT, COMPOSE_WIDTH

    p = composition_service._params({}, {"output_size": {
        "width": 640, "height": 240, "clips": 1, "same_as": 2, "of": 2}})
    assert (p["width"], p["height"]) == (COMPOSE_WIDTH, COMPOSE_HEIGHT)
    assert "默认档" in p["size_basis"]
    odd = composition_service._params({}, {"output_size": {
        "width": 641, "height": 353, "clips": 1, "same_as": 1, "of": 1}})
    assert odd["width"] % 2 == 0 and odd["height"] % 2 == 0, "奇数尺寸不能当输出用"


def test_没有素材时不假装知道尺寸():
    p = composition_service._params({}, None)
    assert "默认档" in p["size_basis"] or "没有可量的素材" in p["size_basis"]


def test_输出尺寸取众数而不是取第一镜():
    from composition.timeline import majority_size

    rows = [{"video": {"width": 640, "height": 352}},
            {"video": {"width": 1280, "height": 720}},
            {"video": {"width": 1280, "height": 720}}]
    assert majority_size(rows) == {"width": 1280, "height": 720, "clips": 2,
                                   "same_as": 2, "of": 3}
    assert majority_size([]) is None
    assert majority_size([{"video": {}}]) is None


def test_时间线快照里冻着当次的尺寸档(stage):
    """事后要能回答"这一版当时是按哪一档拼的"——快照里没这一项就是没记。"""
    for job in stage["jobs"]:
        _approve(job, stage["project_id"], stage["user_id"])
    view = composition_service.create(stage["project_id"], stage["episode_id"],
                                      stage["user_id"])
    worker.run_once(WID)
    frozen = json.loads(comp_row(view["id"])["timeline_json"])
    assert frozen["output_size"] == {"width": 640, "height": 360, "clips": 1,
                                     "same_as": 2, "of": 2}, frozen.get("output_size")
    assert frozen["shots"][0]["video"]["width"] == 640
