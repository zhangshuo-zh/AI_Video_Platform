#!/usr/bin/env python3
"""任务书 §17「必须完成的验收场景」真跑（P0-6）。

为什么单独一个脚本、而不是往 pytest 里再加六个用例：
pytest 的用例按"一块逻辑"切（一个断言盯一个判定），这一套要验的是
**一条业务能不能从头走到尾**——真 HTTP、真 worker 子进程、真产物文件、
真签字、真 FFmpeg 合成。它跑完留下的是一份能对着念的过程记录，
而不是一句"24 passed"。输出直接进
`文档/开发/生产工作台改造/03-业务验收记录.md`，所以每一步都带场景号与实测值。

四条纪律：
1. **不碰开发库**：先把 `data/platform.db` 复制进 `/tmp/scenario_check`，
   所有写入都发生在副本上。
2. **mock 只证明代码路径**：S1—S5 的图像/视频用平台自带的假适配器轨
   （`FASTVIDEO_ALLOW_MOCK=1` 才存在），但它产出的是**真 mp4／真 png**，
   所以"签字 → 时间线取片 → FFmpeg 合成 → 交付"这条链是真跑的；
   `model_validation` 不会因此新增，生产可用徽章也不亮。
3. **S6 用真实模型**：产线派发生成的那一腿走 FastMetal 1.3B（本机已装并实测），
   这是本轮唯一的"真实模型跑通平台 Job"证据。缺权重时用 `--no-real` 跳过。
4. **不花钱**：文本抽取走真代码路径，但把 `DEEPSEEK_BASE_URL` 指到本机一个假
   OpenAI 兼容端点，密钥是一个明显的假值。计费单仍按清单价格记进成本账，
   供应商那边一次请求都没发出。

用法：
    python3 tools/production_scenario_check.py            # 六个场景全跑
    python3 tools/production_scenario_check.py --only 2   # 只跑场景二
    python3 tools/production_scenario_check.py --no-real  # 跳过场景六的真实模型腿
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "app" / "backend"
TMP = Path("/tmp/scenario_check")
PORT = 8012
BASE = f"http://127.0.0.1:{PORT}"
STUB_SECRET = "sk-scenario-stub-not-a-real-key"      # 假值：只用于本机假端点对撞
started_at = time.strftime("%Y-%m-%d %H:%M:%S")

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BACKEND))

results: list[tuple[str, bool, str]] = []
SERVER = None
WORKER = None
STUB = None
TOKEN = ""


# ---------------------------------------------------------------- 断言与打印
def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"  · {str(detail)[:200]}" if detail else ""), flush=True)


def step(title):
    print(f"\n—— {title} ——", flush=True)


# ---------------------------------------------------------------- 环境准备
def prepare_env():
    """必须在 import 后端模块之前设好：config.py 在导入时就把环境变量读成常量。"""
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    shutil.copy(BACKEND / "data" / "platform.db", TMP / "platform.db")
    for suffix in ("-wal", "-shm"):
        src = BACKEND / "data" / f"platform.db{suffix}"
        if src.exists():
            shutil.copy(src, TMP / f"platform.db{suffix}")
    os.environ["FASTVIDEO_DB"] = str(TMP / "platform.db")
    os.environ["FASTVIDEO_FILES"] = str(TMP / "files")
    os.environ["FASTVIDEO_JOBS"] = str(TMP / "jobs")
    os.environ["FASTVIDEO_WORKSPACE"] = str(TMP / "workspace")
    # 假适配器轨：S1—S5 的图像/视频靠它把"队列→产物→签字→合成"跑穿
    os.environ["FASTVIDEO_ALLOW_MOCK"] = "1"
    # 真实模型轨需要的运行时：与 run.sh 同一套推导，不另写一份路径
    if not os.environ.get("FASTVIDEO_FASTMETAL_HOME") and (Path.home() / "FastVideo").is_dir():
        os.environ["FASTVIDEO_FASTMETAL_HOME"] = str(Path.home() / "FastVideo")
    if not os.environ.get("FASTVIDEO_MFLUX_BIN") and (
            Path.home() / ".local/bin/mflux-generate").exists():
        os.environ["FASTVIDEO_MFLUX_BIN"] = str(Path.home() / ".local/bin/mflux-generate")

    from migrate import migrate
    migrate(verbose=False)


def point_text_track_at_stub(port: int):
    """把文本默认轨指到本机假端点：真代码路径、零花费。"""
    import sqlite3
    with sqlite3.connect(TMP / "platform.db") as conn:
        conn.execute("UPDATE provider_config SET is_default = 0 WHERE capability = 'text'")
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'deepseek', model = 'deepseek-flash',
                      enabled = 1, is_default = 1, key_env = 'DEEPSEEK_API_KEY'
                WHERE capability = 'text' AND backend = 'cloud'""")
        conn.commit()
    os.environ["DEEPSEEK_API_KEY"] = STUB_SECRET
    os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"


SCRIPT_TEXT = (
    "掌灯人站在灯市口，手里提着一盏没点的灯。\n"
    "灯市从河岸一直排到城门，夜里没有风。\n"
    "他把灯举过头顶，说：你迟了三年。\n"
)

# 证据引句必须逐字出现在正文里（抽取端的硬规则），所以下面这份回包与 SCRIPT_TEXT 是一对。
STUB_REPLY = {
    "characters": [{
        "name": "掌灯人", "aliases": ["提灯的老者"], "role": "主角",
        "appearance": "身形瘦高，常提一盏没点的灯", "personality": "寡言",
        "suggested_prompt": "瘦高老者，平涂赛璐璐，暖灰主色", "confidence": 0.86,
        "evidence": {"quote": "掌灯人站在灯市口"},
    }],
    "scenes": [{
        "name": "灯市", "time_of_day": "夜", "weather": "无风",
        "description": "河岸到城门的长街，两侧挂满纸灯",
        "suggested_prompt": "夜晚长街，密集纸灯，暖光", "confidence": 0.9,
        "evidence": {"quote": "灯市从河岸一直排到城门"},
    }],
    "props": [{
        "name": "没点的灯", "description": "竹骨纸面，灯芯干枯",
        "suggested_prompt": "竹骨纸灯，未点亮，暖灰调", "confidence": 0.8,
        "evidence": {"quote": "提着一盏没点的灯"},
    }],
    "shots": [{
        "index": 1, "scene": "灯市", "summary": "掌灯人举灯等在灯柱下",
        "shot_size": "中景", "camera_move": "固定",
        "dialogue": ["你迟了三年。"], "duration_hint": 5,
        "asset_refs": ["掌灯人", "灯市"], "confidence": 0.7,
        "evidence": {"quote": "他把灯举过头顶"},
    }],
}


class _StubHandler(BaseHTTPRequestHandler):
    """本机假供应商：`server.mode` 决定回包还是回 500（场景四要它装云端故障）。"""

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.headers.get("Authorization") != f"Bearer {STUB_SECRET}":
            self._json(401, {"error": {"message": "bad key"}})
            return
        if getattr(self.server, "mode", "ok") == "error":
            # 带 request_id 的 5xx 才是"结果未知"：任务可能已经建立、甚至已计费。
            # 不带 id 的 5xx 走的是普通失败——那一笔根本没送到供应商。
            self._json(500, {"error": {"message": "stub outage for scenario 4",
                                       "code": "server_error"},
                             "request_id": "chatcmpl-stub-500-1"})
            return
        self._json(200, {
            "id": "chatcmpl-scenario", "model": "deepseek-flash",
            "choices": [{"message": {"content": json.dumps(self.server.reply,
                                                           ensure_ascii=False)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100},
        })

    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_stub():
    global STUB
    STUB = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    STUB.reply = STUB_REPLY
    STUB.mode = "ok"
    threading.Thread(target=STUB.serve_forever, daemon=True).start()
    return STUB


# ---------------------------------------------------------------- HTTP 客户端
def api(path, method="GET", body=None):
    """返回 (status, data)。负向断言要的就是状态码，所以这里不抛异常。"""
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "Cookie": f"fv_session={TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except ValueError:
            return exc.code, {"detail": raw.decode("utf-8", "ignore")[:300]}
    except Exception as exc:
        return 0, {"detail": f"{type(exc).__name__}: {exc}"}


def must(path, method="GET", body=None, expect=None):
    """默认接受任何 2xx：这个脚本要验的是业务能不能走通，不是某个状态码。
    真要断言"必须 202"或"必须 422"的地方另有其处（负向检查直接调 `api`）。"""
    status, data = api(path, method, body)
    allowed = expect if expect is not None else (200, 201, 202, 204)
    if status not in allowed:
        raise AssertionError(f"{method} {path} → {status}：{str(data)[:200]}")
    return data


def wait_job(job_id, timeout=120, label=""):
    """等一条作业进终态。轮询的是接口，不是数据库——人看到的也是这个接口。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _status, last = api(f"/api/jobs/{job_id}")
        if (last or {}).get("status") in ("succeeded", "failed", "cancelled",
                                          "needs_attention"):
            return last
        time.sleep(0.5)
    return {"status": "timeout", "error_code": "client_timeout",
            "error_message": f"{label or job_id} 在 {timeout}s 内没进终态",
            "id": job_id, **(last or {})}


def submit(capability, params=None, shot_id=None, episode_id=None, project_id=None,
           provider="mock", model="mock-1", mode="local", prompt="验收镜头",
           estimate=None, confirmed=False, key=None):
    body = {"project_id": project_id, "episode_id": episode_id, "shot_id": shot_id,
            "capability": capability, "execution_mode": mode,
            "provider_code": provider, "model_code": model,
            "prompt": prompt, "params": params or {}, "input_refs": []}
    if estimate is not None:
        body["estimate"] = estimate
        body["user_confirmed_cloud"] = bool(confirmed)
    if key:
        body["idempotency_key"] = key
    return must("/api/jobs", "POST", body, expect=(200, 202))


def sign(job_id, note="验收通过"):
    review = must(f"/api/jobs/{job_id}/review")
    anchors = review.get("anchors") or []
    anchor = anchors[0]["anchor"] if anchors and isinstance(anchors[0], dict) else "artifact"
    return must(f"/api/jobs/{job_id}/review-sign", "POST",
                {"anchor": anchor, "decision": "approved", "note": note})


def mock_media(capability, color, project_id, episode_id=None, shot_id=None,
               label="", timeout=90, wait=True):
    """假适配器轨产出一件真媒体（mp4/png）。"""
    params = {"sleep": 0.2, "media": "mp4" if capability == "video" else "png",
              "color": color, "seconds": 1.0}
    job = submit(capability, params=params, project_id=project_id,
                 episode_id=episode_id, shot_id=shot_id, prompt=label or capability)
    if not wait:
        return job
    return wait_job(job["id"], timeout=timeout, label=label or capability)


# ---------------------------------------------------------------- 场景公共件
def new_project(note):
    project = must("/api/projects", "POST",
                   {"name": f"验收·{note}", "description": "", "tags": []})
    episode = must(f"/api/projects/{project['id']}/episodes", "POST", {"title": "第 1 集"})
    must("/api/context", "PUT", {"project_id": project["id"], "episode_id": episode["id"]})
    return project, episode


def add_shot(episode_id, summary, duration=5, asset_ids=None, variant_ids=None):
    return must(f"/api/episodes/{episode_id}/shots", "POST",
                {"summary": summary, "prompt": "崖顶黄昏，暖光", "duration": duration,
                 "asset_ids": asset_ids or [], "variant_ids": variant_ids or []})


def update_shot(shot, **changes):
    body = {k: shot[k] for k in ("summary", "prompt", "duration", "asset_ids",
                                 "variant_ids")}
    body.update(changes)
    return must(f"/api/shots/{shot['id']}", "PUT", body)


def make_asset(project_id, kind, name, summary="一致性：平涂、暖灰主色"):
    return must(f"/api/projects/{project_id}/assets", "POST",
                {"kind": kind, "name": name, "summary": summary,
                 "consistency_prompt": summary, "aliases": [], "tags": [],
                 "scope": "project"})


def media_files():
    return (glob.glob(f"{TMP}/jobs/*/outputs/*.mp4")
            + glob.glob(f"{TMP}/jobs/*/outputs/*.png"))


# ====================================================================== 场景一
def scenario_one(ctx=None):
    step("场景一 从项目到成片：创建项目→集→剧本→解析→资产→分镜→关键帧→视频→审核→合成→交付")

    project, episode = new_project("场景一")
    check("1.1 项目与集建成并钉进上下文", bool(project.get("id") and episode.get("id")),
          f"project={project['id']} episode={episode['id']}")

    doc = must(f"/api/projects/{project['id']}/documents", "POST",
               {"kind": "script", "title": "灯市", "episode_id": episode["id"],
                "content": ""})
    content = must(f"/api/documents/{doc['id']}/content")
    saved = must(f"/api/documents/{doc['id']}/content", "PUT",
                 {"content": SCRIPT_TEXT, "expected_version": content["version"]})
    check("1.2 剧本正文真写进稿子目录并换出版本",
          bool(saved.get("version")) and saved["version"] != content["version"],
          f"版本 {content['version'][:12]}… → {saved['version'][:12]}…")

    options = must(f"/api/projects/{project['id']}/extract/options?doc_id={doc['id']}")
    run = must(f"/api/projects/{project['id']}/extract/runs", "POST", {
        "project_id": project["id"], "doc_id": doc["id"], "instruction": "",
        "estimate": {"quantity": options["estimated_quantity"], "unit": options["unit"],
                     "unit_price_micros": options["unit_price_micros"],
                     "currency": options["currency"], "source": options["source"]},
        "user_confirmed_cloud": True}, expect=(200, 202))
    job = wait_job(run["job_id"], timeout=90, label="抽取")
    check("1.3 解析作业经真队列与真 worker 完成（供应商是本机假端点，零花费）",
          job["status"] == "succeeded",
          f"job={job['id']} {job['provider_code']}/{job['model_code']}"
          f" 用时 {job.get('elapsed_ms')}ms")

    items = must(f"/api/extract/runs/{run['id']}/items")
    kinds = sorted({i["kind"] for i in items})
    assets_before = must(f"/api/projects/{project['id']}/assets")
    check("1.4 候选池分四类，未经确认不建正式资产",
          {"character", "scene", "prop", "shot"} <= set(kinds) and not assets_before,
          f"候选 {len(items)} 条：{kinds}｜资产面 {len(assets_before)} 件")

    created = {}
    for item in items:
        if item["kind"] in ("character", "scene", "prop") \
                and item["status"] in ("pending", "candidate"):
            created[item["kind"]] = must(f"/api/extract/items/{item['id']}/accept", "POST", {})
    check("1.5 人工确认后才落成资产（角色/场景/道具各一）", len(created) == 3,
          "、".join(f"{k}" for k in created))
    assets_now = must(f"/api/projects/{project['id']}/assets")
    check("1.6 资产在项目资产面可见", len(assets_now) >= 3,
          f"{len(assets_now)} 件：{'、'.join(a['name'] for a in assets_now)}")

    char = next((a for a in assets_now if a["kind"] == "character"), None)
    variant = must(f"/api/assets/{char['id']}/variants", "POST",
                   {"variant_type": "outfit", "name": "旧袍", "prompt": "灰布旧袍"},
                   expect=(200, 201))
    scene = next((a for a in assets_now if a["kind"] == "scene"), None)
    shots = [add_shot(episode["id"], f"第 {i + 1} 镜", asset_ids=[char["id"], scene["id"]],
                      variant_ids=[variant["id"]]) for i in range(2)]
    check("1.7 分镜建成并引用人物＋具体形态",
          all(s["variant_ids"] == [variant["id"]] for s in shots),
          f"{len(shots)} 镜，形态 id={variant['id']}")

    video_jobs = []
    for idx, shot in enumerate(shots):
        img = mock_media("image", "red" if idx == 0 else "green", project["id"],
                         episode["id"], shot["id"], label=f"关键帧{idx + 1}")
        vid = mock_media("video", "blue" if idx == 0 else "gray", project["id"],
                         episode["id"], shot["id"], label=f"视频{idx + 1}")
        if img["status"] != "succeeded" or vid["status"] != "succeeded":
            raise AssertionError(f"产物未成功：{img['status']}/{vid['status']}")
        sign(vid["id"])
        video_jobs.append(vid)
    mp4s = glob.glob(f"{TMP}/jobs/*/outputs/*.mp4")
    pngs = glob.glob(f"{TMP}/jobs/*/outputs/*.png")
    check("1.8 关键帧与视频落成真文件（png/mp4 各两件）",
          len(mp4s) >= 2 and len(pngs) >= 2, f"{len(pngs)} 张图／{len(mp4s)} 段视频")

    pre = must(f"/api/projects/{project['id']}/composition-precheck"
               f"?episode_id={episode['id']}")
    check("1.9 合成前预检放行", pre["status"] in ("通过", "警告"),
          f"{pre['status']}｜共 {pre['total']} 项 阻塞 {pre['blocking']} 警告 {pre['warnings']}")

    comp = must(f"/api/projects/{project['id']}/compositions", "POST",
                {"episode_id": episode["id"]}, expect=(200, 201, 202))
    cjob = wait_job(comp["job_id"], timeout=300, label="合成")
    view = must(f"/api/compositions/{comp['id']}")
    out_file = Path(view["output_path"])
    if not out_file.is_absolute():
        out_file = Path(os.environ["FASTVIDEO_JOBS"]) / view["output_path"]
    check("1.10 成片合成成功、落盘、可交付",
          cjob["status"] == "succeeded" and out_file.is_file() and bool(view["sha256"]),
          f"v{view['version']} {view['bytes']}B sha256={str(view['sha256'])[:12]}… "
          f"{view['width']}x{view['height']} {view['duration_ms']}ms")
    check("1.11 数据关系链一次连上（项目→集→镜头→作业→产物→审核→成片）",
          view["episode_id"] == episode["id"] and view["shot_count"] == len(shots)
          and all(j["episode_id"] == episode["id"] for j in video_jobs),
          f"成片含 {view['shot_count']} 镜，音频 {view['audio_count']} 条")

    # 交付这一档不是"文件出来了"就算：成片行没有状态列，签字签在那条合成作业上
    # （`composition_delivery` 数的是 target_kind='job' 的通过签字）。
    sign(cjob["id"], note="成片审核通过")

    payload = must(f"/api/projects/{project['id']}/production")
    board, summary = payload["board"], payload["overview"]
    delivered = [e for e in summary["per_episode"] if e["episode_id"] == episode["id"]]
    check("1.12 项目工作台把这一集认成已交付（镜头数、成片版本、交付标记一起改口）",
          bool(delivered) and delivered[0]["delivered"] is True
          and delivered[0]["shots"] == len(shots)
          and delivered[0]["composition_versions"] >= 1,
          f"第 {delivered[0]['seq'] if delivered else '?'} 集：镜头 {delivered[0]['shots']}"
          f"｜成片 {delivered[0]['composition_versions']} 版｜已交付 {delivered[0]['delivered']}"
          f"｜总进度 {summary['progress']}%")
    stages = {s["key"]: s for s in board["stages"]}
    check("1.13 九阶段一次列全，交付阶段计数跟着成片走",
          len(board["stages"]) == 9 and stages["deliver"]["done"] >= 1,
          "／".join(f"{s['label']}{s['done']}:{s['total']}" for s in board["stages"]))
    return {"project": project, "episode": episode, "shots": shots,
            "character": char, "scene": scene, "variant": variant,
            "doc_id": doc["id"]}


# ====================================================================== 场景二
def scenario_two(ctx):
    step("场景二 单镜头完整制作：进镜头→改描述→选人物与形态→选场景→关键帧→视频→预览→审核→标记可合成")

    project, episode, shot = ctx["project"], ctx["episode"], ctx["shots"][1]
    board = must(f"/api/shots/{shot['id']}/workbench")
    keys = ["setting", "refs", "keyframes", "videos", "audio", "preview",
            "review", "compose_ready"]
    check("2.1 一屏给齐八段（设定/资产/关键帧/视频/音频/预览/审核/可合成）",
          all(k in board for k in keys), "／".join(keys))

    updated = update_shot(
        shot, summary="掌灯人把灯举过头顶，等一个迟到三年的人",
        prompt="夜，灯市长街，人物居中", duration=6,
        asset_ids=[ctx["character"]["id"], ctx["scene"]["id"]],
        variant_ids=[ctx["variant"]["id"]],
        shot_size="中景", camera_move="推", aspect="16:9",
        style="平涂赛璐璐", negative_prompt="多余手指")
    board = must(f"/api/shots/{shot['id']}/workbench")
    check("2.2 镜头设定五栏进生产档案并在同一屏显示",
          board["setting"]["shot_size"] == "中景"
          and board["setting"]["camera_move"] == "推"
          and board["setting"]["duration"] == 6.0,
          f"景别 {board['setting']['shot_size']}／运镜 {board['setting']['camera_move']}"
          f"／比例 {board['setting']['aspect']}／时长 {board['setting']['duration']}s"
          f"／修订 {board['setting']['revision']}")
    refs = board["refs"]
    check("2.3 资产引用段同时列出人物、具体形态与场景",
          len(refs["assets"]) == 2 and len(refs["variants"]) == 1,
          f"资产 {'、'.join(a['name'] for a in refs['assets'])}"
          f"｜形态 {'、'.join(v['name'] for v in refs['variants'])}")

    keyframe = mock_media("image", "white", project["id"], episode["id"], shot["id"],
                          label="关键帧")
    video = mock_media("video", "black", project["id"], episode["id"], shot["id"],
                       label="视频")
    board = must(f"/api/shots/{shot['id']}/workbench")
    check("2.4 关键帧与视频两条记录在同一屏列出（不切模块）",
          len(board["keyframes"]["jobs"]) >= 1 and len(board["videos"]["jobs"]) >= 1,
          f"关键帧 {len(board['keyframes']['jobs'])} 条／视频 {len(board['videos']['jobs'])} 条")

    check("2.5 未签字前不认正式视频", board["videos"]["formal_job_id"] != video["id"],
          f"正式视频 id={board['videos']['formal_job_id']}")
    sign(video["id"], note="单镜头验收通过")
    board = must(f"/api/shots/{shot['id']}/workbench")
    check("2.6 签字后正式视频被工作台认下，预览给出可回放产物",
          board["videos"]["formal_job_id"] == video["id"]
          and board["preview"]["formal_video"] is not None,
          f"正式视频 job={board['preview']['formal_video']['job_id']}")
    ready = board["compose_ready"]
    check("2.7 这一镜标记为可参与合成", ready["can_compose"] is True,
          f"阻塞 {len(ready['blockers'])} 项｜视频 {ready['video_ok']}"
          f"｜签字 {ready['signed_ok']}｜源版本 {ready['source_ok']}")
    engines = board["audio"]["engines"]
    wired = any(v.get("runner_wired") for v in engines.values())
    check("2.8 未接入的音频能力照实说未接入，不给会失败的提交入口",
          wired is False and bool(board["audio"]),
          f"{'、'.join(engines)} 全部 runner_wired=false"
          if not wired else "有引擎接上了（本轮结论需改）")
    return {"two": {"shot": shot, "video_job": video, "keyframe_job": keyframe}}


# ====================================================================== 场景三
def scenario_three(ctx):
    step("场景三 资产发生变化：改人物一致性描述→识别受影响镜头→产物判过期→重做后恢复")

    project, shot = ctx["project"], ctx["two"]["shot"]
    char = ctx["character"]
    before = must(f"/api/assets/{char['id']}")
    must(f"/api/assets/{char['id']}", "PUT",
         {k: before[k] for k in ("kind", "name", "summary", "consistency_prompt",
                                 "aliases", "tags")} |
         {"scope": "project",
          "consistency_prompt": "一致性：瘦高老者，改为青灰主色、加围巾"})

    impact = must(f"/api/assets/{char['id']}/impact")
    hit = next((s for s in impact["shots"] if s["shot_id"] == shot["id"]), None)
    check("3.1 改动能点名受影响的镜头与所在集",
          hit is not None and hit["episode_id"] == ctx["episode"]["id"],
          f"受影响 {len(impact['shots'])} 镜｜本镜＝第 {hit['seq'] if hit else '?'} 镜"
          f"｜计数 {json.dumps(impact['counts'], ensure_ascii=False)}")
    check("3.2 该镜最新视频被判定为源已变，并给出建议",
          hit is not None and hit["stale"] is True and hit["recommend"],
          f"{hit['recommend'] if hit else '—'}")

    board = must(f"/api/shots/{shot['id']}/workbench")
    check("3.3 工作台把这一镜从「可合成」退回「需要处理」",
          board["compose_ready"]["can_compose"] is False
          and board["videos"]["source_stale"] is True,
          f"阻塞：{'／'.join(b['text'] for b in board['compose_ready']['blockers'])[:120]}"
          or "can_compose=false")

    pre = must(f"/api/precheck/shot/{shot['id']}")
    mentions = [i for i in pre["items"] if "源" in i["text"]]
    check("3.4 生成前预检点名源版本问题", bool(mentions),
          f"{pre['status']}｜{mentions[0]['text'][:80] if mentions else '没提到源版本'}")

    vid = mock_media("video", "red", project["id"], ctx["episode"]["id"], shot["id"],
                     label="重生成")
    sign(vid["id"], note="按新一致性口径重做后通过")
    board = must(f"/api/shots/{shot['id']}/workbench")
    check("3.5 重做并签字后重新回到可合成",
          board["compose_ready"]["can_compose"] is True,
          f"正式视频 job={board['videos']['formal_job_id']}")
    _status, history = api(f"/api/jobs?project_id={project['id']}&shot_id={shot['id']}"
                           f"&limit=50")
    rows = history.get("items", history) if isinstance(history, dict) else history
    check("3.6 历史不覆盖：旧产物与旧签字都还在，这一镜留有多条作业",
          len(rows or []) >= 3,
          f"{len(rows or [])} 条作业（含改前与改后）")
    return {"regen_job": vid}


# ====================================================================== 场景四
def scenario_four(ctx):
    step("场景四 生成失败恢复：模型不可用／worker 离线／任务超时／云端状态未知")

    project, episode, shot = ctx["project"], ctx["episode"], ctx["two"]["shot"]

    # 「模型不可用」这一类怎么造：不能凭空指定一个没在配置里的槽位
    # （平台硬规则：提交时指定的 provider/模型与配置槽位不一致就 422，见
    # `services.submit_job`）。所以这里把 worker 的运行时目录指到一个不存在的
    # 位置再起——**配置里那条真槽位**（fastmetal/fastmetal-1.3b-qad）于是缺权重，
    # 领取后以 adapter_unavailable 失败，报的正是人要去修环境时该看到的那句话。
    stop_worker()
    start_worker(extra_env={"FASTVIDEO_FASTMETAL_HOME": str(TMP / "没有这个目录")})
    dead = submit("video", params={}, project_id=project["id"],
                  episode_id=episode["id"], shot_id=shot["id"],
                  provider="fastmetal", model="fastmetal-1.3b-qad", mode="local",
                  prompt="运行时目录不见了")
    failed = wait_job(dead["id"], timeout=60, label="缺运行时")
    check("4.1 模型不可用时以明确错误码失败，并说清缺哪件",
          failed["status"] == "failed"
          and failed.get("error_code") == "adapter_unavailable",
          f"{failed.get('error_code')}｜{str(failed.get('error_message'))[:90]}")
    retried = must(f"/api/jobs/{dead['id']}/retry", "POST", {}, expect=(200, 202))
    old = must(f"/api/jobs/{dead['id']}")
    check("4.2 重试新建一行，原行的错误与耗时一个字段没动",
          retried["id"] != dead["id"] and old["status"] == "failed"
          and old.get("error_code") == "adapter_unavailable",
          f"原 #{dead['id']} 仍是 {old['status']}／新 #{retried['id']}")

    stop_worker()
    queued = mock_media("video", "gray", project["id"], episode["id"], shot["id"],
                        wait=False)
    # 在线判定有 20 秒心跳窗口（WORKER_ONLINE_SECONDS），进程刚死那一刻
    # 界面还不能说它离线——所以要等到窗口过，而不是假设"停了就说离线"。
    ws, offline = {}, False
    for _ in range(45):
        _s, ws = api("/api/worker/status")
        offline = ws.get("online") is False
        if offline:
            break
        time.sleep(1.0)
    stayed = must(f"/api/jobs/{queued['id']}")["status"]
    check("4.3 worker 离线后心跳窗口一过就报离线，作业仍停在队列里不永挂",
          stayed == "queued" and offline,
          f"作业状态 {stayed}｜worker {json.dumps(ws, ensure_ascii=False)[:130]}")
    start_worker()
    back = wait_job(queued["id"], timeout=120, label="worker 回来后")
    check("4.4 worker 回来后自动接上，不需要人工重提",
          back["status"] == "succeeded", f"{back['status']} 用时 {back.get('elapsed_ms')}ms")

    # 超时这一类必须用**真适配器**造：假适配器的循环只看取消与停止信号，
    # 不受 MAX_RUNTIME 约束（`_run_mock` 里没有时长判断），拿它测超时只会测到
    # 一个永远不发生的成功。真轨（FastMetal）起的是子进程，超时会被 SIGTERM 收掉。
    stop_worker()
    slow = submit("video", {}, shot_id=shot["id"], episode_id=episode["id"],
                  project_id=project["id"], provider="fastmetal",
                  model="fastmetal-1.3b-qad", mode="local", prompt="超时演示")
    start_worker(max_runtime=5)
    timed = wait_job(slow["id"], timeout=120, label="超时")
    check("4.5 超时有独立错误码，不被误报成模型故障",
          timed.get("error_code") == "run_timeout",
          f"{timed['status']}｜{timed.get('error_code')}｜{str(timed.get('error_message'))[:80]}")
    # 立刻把 worker 换回正常时长：留着 5 秒上限跑下去，后面的真模型作业会被误判成超时
    stop_worker()
    start_worker()

    STUB.mode = "error"
    try:
        # 抽取有 25 小时同文同模型去重：正文不换一版，提交会复用上一次成功的 run，
        # 于是"云端故障"这条根本不会被走到。先追加一行，再提交。
        current = must(f"/api/documents/{ctx['doc_id']}/content")
        must(f"/api/documents/{ctx['doc_id']}/content", "PUT",
             {"content": current["content"] + "\n灯芯爆了一下。\n",
              "expected_version": current["version"]})
        options = must(f"/api/projects/{project['id']}/extract/options?doc_id={ctx['doc_id']}")
        run = must(f"/api/projects/{project['id']}/extract/runs", "POST", {
            "project_id": project["id"], "doc_id": ctx["doc_id"], "instruction": "",
            "estimate": {"quantity": options["estimated_quantity"],
                         "unit": options["unit"],
                         "unit_price_micros": options["unit_price_micros"],
                         "currency": options["currency"], "source": options["source"]},
            "user_confirmed_cloud": True}, expect=(200, 202))
        uncertain = wait_job(run["job_id"], timeout=90, label="云端故障")
        detail = must(f"/api/jobs/{uncertain['id']}")
        check("4.6 云端结果未知时进「需要处理／待核对」，不判失败、不自动重试",
          detail["status"] == "needs_attention"
          and detail.get("billable_status") == "pending_reconciliation"
          and not str(detail.get("error_code")).startswith("provider_provider_"),
          f"{detail['status']}／{detail.get('billable_status')}｜"
          f"{detail.get('error_code')}｜回执 {detail.get('provider_request_id')}")
    finally:
        STUB.mode = "ok"


def _find_image_local_slot(node):
    """在清单里递归找 image/local 那一档：provider 码与第一个模型码。

    不写死"mflux/flux1-schnell"这两个字符串：清单是唯一来源（规格住代码），
    工具跟着它走，将来换档或改名时这里不会悄悄说谎。
    """
    if isinstance(node, dict):
        if node.get("capability") == "image" and node.get("backend") == "local":
            models = node.get("models") or []
            code = node.get("code") or node.get("provider")
            if code and models:
                first = models[0]
                return code, (first.get("code") if isinstance(first, dict) else first)
        for value in node.values():
            got = _find_image_local_slot(value)
            if got:
                return got
    elif isinstance(node, list):
        for value in node:
            got = _find_image_local_slot(value)
            if got:
                return got
    return None


def set_image_default_track():
    """配上图像本地默认轨——走的是真用户路径（GET + PUT /api/providers）。

    为什么要现配：副本库里 `image/local` 那行的 provider 与 model 是空的，
    关键帧节点因此解析不出路线、只能失败。场景六要跑到真关键帧，
    就得把"人怎么修的"这一步也走一遍，而不是偷偷 UPDATE 一行假装它本来就好。
    返回 False＝这台机器现在配不出来（清单里没这一档或 PUT 被规则拒），
    场景六据此如实少报一条，而不是把检查改成永远通过。
    """
    slot = _find_image_local_slot(must("/api/providers/manifest"))
    if slot is None:
        return False
    provider, model = slot
    body = []
    for r in must("/api/providers"):
        item = {k: r[k] for k in ("id", "provider", "model", "enabled", "is_default")}
        item["key_env"] = r.get("key_env_custom", "")
        item["notes"] = r.get("notes", "")
        if r["capability"] == "image" and r["backend"] == "local":
            item.update(provider=provider, model=model, enabled=1, is_default=1)
        elif r["capability"] == "image":
            item["is_default"] = 0
        body.append(item)
    status, data = api("/api/providers", "PUT", body)
    if status not in (200, 201):
        print(f"     （图像默认轨没配上：HTTP {status} {str(data)[:140]}）", flush=True)
        return False
    return True


# ====================================================================== 场景五
def scenario_five(ctx=None):
    step("场景五 合成前预检：缺视频／未审核／源版本变化／字幕／环境 都要能定位到具体镜头")

    project, episode = new_project("场景五")
    shots = [add_shot(episode["id"], f"阻塞第 {i + 1} 镜") for i in range(3)]

    v2 = mock_media("video", "blue", project["id"], episode["id"], shots[1]["id"],
                    label="有片未签字")
    v3 = mock_media("video", "green", project["id"], episode["id"], shots[2]["id"],
                    label="签字后被改")
    sign(v3["id"], note="先签一版")
    changed = update_shot(shots[2], summary="改了内容：这一段旧素材不该再用")
    shots[2] = changed

    pre = must(f"/api/projects/{project['id']}/composition-precheck"
               f"?episode_id={episode['id']}")
    blockers = [i for i in pre["items"] if i["level"] == "阻塞"]
    texts = "｜".join(i["text"] for i in blockers)
    located = {
        "缺视频": "没有成功" in texts or "没有" in texts,
        "未审核": "审核" in texts or "签字" in texts,
        "源版本变化": "源" in texts,
    }
    check("5.1 一次把三类阻塞全列出来", all(located.values()),
          f"{located}｜阻塞 {pre['blocking']} 条")
    check("5.2 每条阻塞都带可定位对象（镜头号或产物 id）",
          bool(blockers) and all(i.get("locate") for i in blockers),
          f"示例定位：{json.dumps(blockers[0]['locate'], ensure_ascii=False)}"
          if blockers else "没有阻塞项")
    check("5.3 结论是「阻塞」，数量一次给全",
          pre["status"] == "阻塞",
          f"共 {pre['total']} 项／阻塞 {pre['blocking']}／警告 {pre['warnings']}")

    rejected = api(f"/api/projects/{project['id']}/compositions", "POST",
                   {"episode_id": episode["id"]})
    check("5.4 阻塞时发起合成被当场拒绝（不是排队后再失败）",
          rejected[0] == 422, f"HTTP {rejected[0]}｜{str(rejected[1])[:120]}")

    sign(v2["id"], note="补签")
    for sid in (shots[1]["id"], shots[2]["id"]):
        fresh = mock_media("video", "black", project["id"], episode["id"], sid,
                           label="重做")
        sign(fresh["id"], note="重做后通过")
    first = mock_media("video", "white", project["id"], episode["id"], shots[0]["id"],
                       label="第 1 镜补上")
    sign(first["id"], note="第 1 镜通过")

    pre2 = must(f"/api/projects/{project['id']}/composition-precheck"
                f"?episode_id={episode['id']}")
    check("5.5 逐条解决后预检放行（允许留警告）",
          pre2["status"] in ("通过", "警告"),
          f"{pre2['status']}｜共 {pre2['total']} 项 阻塞 {pre2['blocking']}"
          f" 警告 {pre2['warnings']}")
    warns = [i for i in pre2["items"] if i["level"] == "警告"]
    check("5.6 警告项也写清是哪一类依赖（音频/字幕/环境）",
          all(i["text"] for i in warns),
          f"警告 {len(warns)} 条：{'／'.join(i['code'] for i in warns)[:120]}")

    comp = must(f"/api/projects/{project['id']}/compositions", "POST",
                {"episode_id": episode["id"]}, expect=(200, 201, 202))
    wait_job(comp["job_id"], timeout=300, label="合成")
    v1 = must(f"/api/compositions/{comp['id']}")

    comp2 = must(f"/api/projects/{project['id']}/compositions", "POST",
                 {"episode_id": episode["id"]}, expect=(200, 201, 202))
    wait_job(comp2["job_id"], timeout=300, label="二次合成")
    v2c = must(f"/api/compositions/{comp2['id']}")
    listed = must(f"/api/projects/{project['id']}/compositions?episode_id={episode['id']}")
    ids = {c["id"] for c in (listed.get("items", listed) if isinstance(listed, dict)
                             else listed)}
    check("5.7 第二次是新版本，旧成片记录与旧文件都还在",
          v2c["version"] == v1["version"] + 1 and {v1["id"], v2c["id"]} <= ids,
          f"v{v1['version']} → v{v2c['version']}｜历史 {len(ids)} 个版本")

    sub = api(f"/api/projects/{project['id']}/composition-subtitles")
    check("5.8 字幕这一项按这台机器的能力给结论（有文件也要说烧不烧得动）",
          sub[0] == 200 and "subtitle_supported" in (sub[1] or {}),
          f"本机可烧字幕={sub[1].get('subtitle_supported')}"
          f"｜{str(sub[1].get('ffmpeg_hint'))[:60]}")
    return {}


# ====================================================================== 场景六
def scenario_six(ctx=None):
    step("场景六 产线自动生产：模板→依赖检查→执行（真实模型）→暂停/继续→人工审核后继续→节点重试→不重复生成")

    if os.environ.get("SCENARIO_NO_REAL") == "1":
        check("6.0 真实模型腿按 --no-real 跳过", True, "本轮未跑真模型")
        return

    project, episode = new_project("场景六")
    shot = add_shot(episode["id"], "掌灯人举灯等在灯柱下", duration=5)
    templates = must("/api/pipeline/templates")
    video_only = next((t for t in templates if t["name"] == "视频生产"), None)
    check("6.1 产线模板可见", video_only is not None,
          f"{len(templates)} 份模板，选用「视频生产」v{video_only['version'] if video_only else '—'}")

    pf = must(f"/api/projects/{project['id']}/preflight?episode_id={episode['id']}")
    check("6.2 生产前检查真给结论", "blocking" in pf and "warnings" in pf,
          f"阻塞 {len(pf['blocking'])}／警告 {len(pf['warnings'])}"
          f"｜依据 {json.dumps(pf.get('info', {}), ensure_ascii=False)[:120]}")

    run = must(f"/api/projects/{project['id']}/pipeline/runs", "POST",
               {"episode_id": episode["id"], "template_id": video_only["id"]},
               expect=(200, 201))
    check("6.3 运行建成并冻结节点", bool(run.get("nodes")),
          f"run={run['id']} {run['status']} 节点 {len(run['nodes'])} 个")

    must(f"/api/pipeline/runs/{run['id']}/advance", "POST", {})
    again = must(f"/api/pipeline/runs/{run['id']}/advance", "POST", {})
    jobs_of = [n["job_id"] for n in again["nodes"] if n.get("job_id")]
    check("6.4 连点两次推进不重复发作业（幂等键挡住）",
          len(jobs_of) == len(set(jobs_of)) and len(jobs_of) >= 1,
          f"节点挂了 {len(jobs_of)} 条作业，去重后 {len(set(jobs_of))} 条")

    video_node = next((n for n in again["nodes"]
                       if n["node_type"] == "video_generate" and n.get("job_id")), None)
    if video_node:
        real = wait_job(video_node["job_id"], timeout=1800, label="真实模型视频")
        check("6.5 产线派发的是真实模型轨（FastMetal 1.3B），不是假适配器",
              real.get("provider_code") == "fastmetal",
              f"{real.get('provider_code')}/{real.get('model_code')} → {real['status']}"
              f"｜{real.get('elapsed_ms')}ms｜{str(real.get('error_message'))[:70]}")
        if real["status"] == "succeeded":
            art = must(f"/api/jobs/{real['id']}/artifacts")
            path = Path(os.environ["FASTVIDEO_JOBS"]) / art[0]["path"]
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True)
            check("6.6 真实模型产物是真视频文件（ffprobe 读得出尺寸）",
                  probe.returncode == 0 and "640,352" in probe.stdout,
                  f"{art[0]['path']}｜{probe.stdout.strip()}｜{art[0]['size']}B")

    paused = must(f"/api/pipeline/runs/{run['id']}/pause", "POST", {})
    resumed = must(f"/api/pipeline/runs/{run['id']}/resume", "POST", {})
    check("6.7 能暂停也能继续，状态如实反映",
          paused["status"] == "paused" and resumed["status"] != "paused",
          f"{paused['status']} → {resumed['status']}")

    latest = must(f"/api/pipeline/runs/{run['id']}")
    gate = next((n for n in latest["nodes"] if n["node_type"] == "shot_review"), None)
    video_job_id = video_node["job_id"] if video_node else None
    # `shot_review` 是人工关卡，它自己不建作业：它盯的是同一镜那条视频作业的签字。
    # 所以这里签的是上游那条视频——签完再推进，这一格才收口。
    sign_target = (gate.get("job_id") if gate else None) or video_job_id
    if gate and sign_target:
        before_status = gate["status"]
        sign(sign_target, note="产线人工审核通过")
        after = must(f"/api/pipeline/runs/{run['id']}/advance", "POST", {})
        done = next((n for n in after["nodes"] if n["node_type"] == "shot_review"), None)
        check("6.8 关键节点卡在人工审核，签字后才放行",
              done is not None and done["status"] in ("succeeded", "completed", "skipped"),
              f"审核节点 {before_status} → {done['status'] if done else '?'}"
              f"（签的是上游视频作业 #{sign_target}）")

    # 失败节点单独重试：拿"图像默认轨还没配"这一格造一次真失败——
    # 它失败的原因是真的（这台机器的图像槽位确实没配 provider），
    # 而重试接口只该动那一格，别的节点作业不能跟着重发。
    image_tpl = next((t for t in templates if t["name"] == "图片预览生产"), None)
    if image_tpl:
        project2, episode2 = new_project("场景六·失败节点")
        add_shot(episode2["id"], "灯柱下")
        bad = must(f"/api/projects/{project2['id']}/pipeline/runs", "POST",
                   {"episode_id": episode2["id"], "template_id": image_tpl["id"]},
                   expect=(200, 201))
        for _ in range(3):
            must(f"/api/pipeline/runs/{bad['id']}/advance", "POST", {})
        failed_nodes = [n for n in must(f"/api/pipeline/runs/{bad['id']}")["nodes"]
                        if n["status"] == "failed"]
        check("6.10 依赖不满足时那一格明确失败并带可读原因",
              bool(failed_nodes),
              f"失败节点：{'、'.join(n['label'] for n in failed_nodes)}"
              f"｜{str(failed_nodes[0].get('error_message'))[:110]}"
              if failed_nodes else "没有失败节点（默认轨已配好？）")
        if failed_nodes:
            target = failed_nodes[0]
            before_map = {n["id"]: n.get("job_id")
                          for n in must(f"/api/pipeline/runs/{bad['id']}")["nodes"]}
            must(f"/api/pipeline/nodes/{target['id']}/retry", "POST", {},
                 expect=(200, 201))
            after_run = must(f"/api/pipeline/runs/{bad['id']}")
            after_map = {n["id"]: n.get("job_id") for n in after_run["nodes"]}
            touched = [nid for nid in before_map
                       if before_map[nid] != after_map[nid] and nid != target["id"]]
            retried_now = next(n for n in after_run["nodes"] if n["id"] == target["id"])
            check("6.11 单节点重试只重建那一格，其他节点的作业一条都没被重新发过",
                  not touched and retried_now["status"] != "succeeded",
                  f"被改动的其他节点 {len(touched)} 个｜重试那一格现在 "
                  f"{retried_now['status']}（默认轨仍未配，允许再次失败）")

            # 再把"原因"真的修掉一次：配上图像默认轨 → 重试 → 这一格跑出真产物。
            # 这一条走的是真模型（mflux / FLUX.1-schnell Q4，本机 2026-11-05 已装并实测）。
            if os.environ.get("SCENARIO_NO_REAL") != "1" and set_image_default_track():
                must(f"/api/pipeline/runs/{bad['id']}/advance", "POST", {})
                must(f"/api/pipeline/nodes/{retried_now['id']}/retry", "POST", {},
                     expect=(200, 201))
                again = must(f"/api/pipeline/runs/{bad['id']}")
                kf = next((n for n in again["nodes"]
                           if n["node_type"] == "image_generate" and n.get("job_id")), None)
                if kf:
                    img_job = wait_job(kf["job_id"], timeout=1200, label="真实图像模型")
                    arts = must(f"/api/jobs/{img_job['id']}/artifacts")
                    path = Path(os.environ["FASTVIDEO_JOBS"]) / arts[0]["path"]
                    check("6.12 修好原因后重试那一格，真模型产出真关键帧",
                          img_job["status"] == "succeeded" and path.is_file(),
                          f"{img_job.get('provider_code')}/{img_job.get('model_code')}"
                          f" → {img_job['status']}｜{arts[0]['path']}"
                          f"｜{arts[0]['size']}B｜{img_job.get('elapsed_ms')}ms")
                else:
                    check("6.12 修好原因后重试那一格，真模型产出真关键帧", False,
                          "重试后关键帧节点仍没挂上作业")
    events = must(f"/api/pipeline/runs/{run['id']}")["events"]
    check("6.9 运行留下可读的事件流（派活、执行、等待人工都在里面）",
          len(events) >= 4,
          f"{len(events)} 条事件｜最近：{'、'.join(e['event_type'] for e in events[-5:])}")
    return {}


# ---------------------------------------------------------------- 进程管理
def start_server():
    """起服务。stderr 落进日志文件：接口 500 时人要能看到 traceback，
    而不是只看到 `Internal Server Error` 这一句——验收工具不留案底就没法定责。"""
    global SERVER
    log = open(TMP / "server.log", "ab")
    SERVER = subprocess.Popen(
        [str(BACKEND / ".venv/bin/python"), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BACKEND), stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        if api("/api/health")[0] == 200:
            return
        time.sleep(0.5)
    raise SystemExit("服务没起来（端口 " + str(PORT) + "）")


def start_worker(max_runtime=None, extra_env=None):
    """起真 worker 子进程。超时那条要单独限时长，缺运行时那条要能改环境——
    两档都通过覆盖子进程环境变量来造，不碰开发库、也不改配置里的槽位。"""
    global WORKER
    env = dict(os.environ)
    if max_runtime:
        env["FASTVIDEO_WORKER_MAX_RUNTIME"] = str(max_runtime)
    if extra_env:
        env.update(extra_env)
    WORKER = subprocess.Popen(
        [str(BACKEND / ".venv/bin/python"), "worker.py", "--poll", "0.2"],
        cwd=str(BACKEND), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(1.5)


def stop_worker():
    global WORKER
    if WORKER and WORKER.poll() is None:
        WORKER.send_signal(signal.SIGTERM)
        try:
            WORKER.wait(timeout=25)
        except subprocess.TimeoutExpired:
            WORKER.kill()
    WORKER = None
    time.sleep(0.5)


def mint_token():
    import sqlite3
    from datetime import datetime, timedelta
    from repositories.users import sessions
    from security import new_session_token

    with sqlite3.connect(TMP / "platform.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT u.id FROM app_user u
                     JOIN user_role ur ON ur.user_id = u.id
                     JOIN role r ON r.id = ur.role_id
                    WHERE r.code = 'admin' AND u.is_active = 1
                   ORDER BY u.is_superuser DESC, u.id LIMIT 1""").fetchone()
        if row is None:
            raise SystemExit("副本库里没有 admin 账号")
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, row["id"], expires)
    return token


SCENARIOS = [("场景一", scenario_one), ("场景二", scenario_two), ("场景三", scenario_three),
             ("场景四", scenario_four), ("场景五", scenario_five), ("场景六", scenario_six)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, help="只跑第 N 个场景（1—6）")
    parser.add_argument("--no-real", action="store_true",
                        help="跳过场景六的真实模型腿（没装权重时用）")
    args = parser.parse_args()
    if args.no_real:
        os.environ["SCENARIO_NO_REAL"] = "1"

    global TOKEN
    prepare_env()
    start_stub()
    point_text_track_at_stub(STUB.server_address[1])
    start_server()
    TOKEN = mint_token()
    start_worker()

    ctx: dict = {}
    try:
        for index, (name, fn) in enumerate(SCENARIOS, start=1):
            if args.only and args.only != index:
                continue
            print(f"\n===== {name} =====", flush=True)
            try:
                out = fn(ctx)
                if out:
                    ctx.update(out)
            except AssertionError as exc:
                check(f"{name} 中断", False, str(exc))
            except Exception as exc:
                check(f"{name} 抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        stop_worker()
        if SERVER and SERVER.poll() is None:
            SERVER.terminate()
        STUB.shutdown()

    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"\n开始 {started_at}｜结束 {time.strftime('%Y-%m-%d %H:%M:%S')}"
          f"｜副本库 {TMP}/platform.db（开发库未动）")
    print(f"{passed}/{len(results)} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name}  · {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
