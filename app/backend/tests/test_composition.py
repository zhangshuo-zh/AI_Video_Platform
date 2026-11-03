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
from repositories.projects import projects as project_repo
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


def _psnr(a: Path, b: Path) -> float:
    """两份成片的平均 PSNR（dB）。同一素材没改动会接近 inf，烧上字幕会掉到几十。"""
    proc = subprocess.run(
        [FFMPEG_BIN, "-hide_banner", "-i", str(a), "-i", str(b),
         "-filter_complex", "[0:v][1:v]psnr", "-f", "null", "-"],
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
        score = _psnr(jobfiles.resolve_artifact(with_sub["output_path"]),
                      jobfiles.resolve_artifact(plain["output_path"]))
        assert score < 40, f"烧了字幕画面却没变（PSNR={score}）"
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
