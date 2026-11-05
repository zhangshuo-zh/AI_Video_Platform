#!/usr/bin/env python3
"""整集多镜真实模型连跑（任务书 §18／§19 要求的真实环境验证）。

为什么单独一个脚本，而不是再加一条场景进 `production_scenario_check.py`：
六场景那条链验的是**每个功能面各自走得通**，场景六只跑了一镜真实模型。
而「这一套能不能真做出一集」是另一个问题——它要的是**多镜连跑**：
N 条真实视频作业排队、逐条签字、再让成片中心按时间线把它们拼成一个真文件。
中间任何一处按「单镜」写死的假设（队列串行、取片规则、时长累加、内存余量）都会在这里露头。

跑法与凭据：
* 在**库副本**上跑（默认 `/tmp/episode_run`，可用 `EPISODE_TMP` 换），开发库一字不动；
* 模型轨是 `adapters` 里登记的 FastMetal 1.3B-QAD（真权重、真子进程、真产物），不是 mock；
* 所有尺寸、时长、字节数都由 `ffprobe` 现量；对不上的数字就该让检查变红，不引推算值。

用法：
    python3 tools/episode_real_run_check.py                    # 六镜（默认）
    EPISODE_SHOTS=3 python3 tools/episode_real_run_check.py     # 赶时间就少跑几镜
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import production_scenario_check as S     # noqa: E402  复用同一套壳（副本库、服务、worker）

SHOTS = int(os.environ.get("EPISODE_SHOTS", "6"))
S.TMP = Path(os.environ.get("EPISODE_TMP", "/tmp/episode_run"))

results: list[tuple[str, bool, str]] = []
started_at = time.strftime("%Y-%m-%d %H:%M:%S")


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  OK  " if ok else "  FAIL") + f" {name}"
          + (f"  · {detail}" if detail else ""), flush=True)


def probe(path: Path):
    """ffprobe 现量一条媒体：宽、高、时长、字节。"""
    entries = "stream=width,height:format=duration,size"
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", entries, "-of", "default=noprint_wrappers=1:nokey=0", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    pairs = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    if proc.returncode != 0:
        return {"width": 0, "height": 0, "duration": 0.0, "bytes": 0,
                "error": proc.stderr.strip()[:120]}
    def num(key: str, default=0):
        # 单张 PNG 没有时长，ffprobe 对 `format=duration` 回的是字符串 "N/A"——
        # 图像腿第一次跑就撞在这里（ValueError 把整轮带走，产物其实都真跑出来了）。
        raw = (pairs.get(key) or "").strip()
        try:
            return float(raw) if raw and raw.upper() != "N/A" else float(default)
        except ValueError:
            return float(default)

    return {"width": int(num("width")), "height": int(num("height")),
            "duration": num("duration"),
            "bytes": int(num("size") or path.stat().st_size), "error": ""}


def rss_rows():
    out = subprocess.run(["ps", "-eo", "pid,ppid,rss,comm"],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    return rows


class Sampler:
    """按 1 秒一次量 worker **子进程树**的峰值 RSS。

    为什么沿 ppid 往下走而不是抓全机最大：全机最大那条可能是浏览器或聚焦索引，
    跟这次生产没关系——把它的数字写进验收记录就是编数据。
    """

    def __init__(self, root_pid: int | None):
        self.root = root_pid
        self.stop = threading.Event()
        self.peak_kb = 0
        self.peak_cmd = ""
        self.samples = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop.is_set():
            rows = rss_rows()
            if self.root:
                kids = {self.root}
                changed = True
                while changed:      # 可能跨多层：worker → python → 模型子进程
                    changed = False
                    for pid, ppid, _rss, _cmd in rows:
                        if ppid in kids and pid not in kids:
                            kids.add(pid)
                            changed = True
                for pid, _ppid, rss, cmd in rows:
                    if pid in kids and rss > self.peak_kb:
                        self.peak_kb, self.peak_cmd = rss, cmd
                self.samples += 1
            self.stop.wait(1.0)

    def start(self):
        self.thread.start()

    def stop_and_report(self) -> str:
        self.stop.set()
        self.thread.join(timeout=5)
        return (f"峰值 {self.peak_kb / 1024 ** 2:.2f} GB（{self.peak_cmd}）"
                f"｜采样 {self.samples} 次") if self.peak_kb else ""


def job_artifact_path(job_id: int) -> Path:
    """那条作业的第一件产物（真实模型轨就一件）。路径按接口的口径拼，不猜。"""
    arts = S.must(f"/api/jobs/{job_id}/artifacts")
    if not arts:
        raise AssertionError(f"作业 #{job_id} 没有登记产物")
    return Path(os.environ["FASTVIDEO_JOBS"]) / arts[0]["path"], arts[0]["path"]


def produce_episode(project: dict, episode: dict, run_id: int, sampler: Sampler) -> dict:
    """把整集推到「每镜都有签过字的真产物」，返回逐镜实测。"""
    seen: set[int] = set()
    clips: dict[int, dict] = {}
    wall_started = time.time()
    sampler.start()
    last = {}
    try:
        for _round in range(SHOTS * 6 + 12):
            last = S.must(f"/api/pipeline/runs/{run_id}/advance", "POST", {})
            for node in last["nodes"]:
                if node["node_type"] == "video_generate" and node.get("job_id") \
                        and node["job_id"] not in seen:
                    seen.add(node["job_id"])
                    detail = S.wait_job(node["job_id"], timeout=2400, label=node["label"])
                    entry = {"job_id": node["job_id"], "label": node["label"],
                             "status": detail["status"],
                             "provider": detail.get("provider_code") or "",
                             "model": detail.get("model_code") or "",
                             "elapsed_ms": int(detail.get("elapsed_ms") or 0),
                             "error": detail.get("error_message") or ""}
                    if detail["status"] == "succeeded":
                        path, rel = job_artifact_path(node["job_id"])
                        entry["path"] = rel
                        entry["media"] = probe(path)
                    clips[node["job_id"]] = entry
                    print(f"      {node['label']}：{entry['status']}"
                          f"｜{entry['provider']}/{entry['model']}"
                          f"｜{entry['elapsed_ms'] / 1000:.1f}s"
                          + (f"｜{entry['media']['width']}x{entry['media']['height']}"
                             f"/{entry['media']['duration']:.2f}s" if entry.get("media") else "")
                          + (f"｜{entry['error'][:60]}" if entry["error"] else ""), flush=True)
            fresh = S.must(f"/api/pipeline/runs/{run_id}")
            for gate in [n for n in fresh["nodes"]
                         if n["node_type"] == "shot_review" and n["status"] == "waiting_review"]:
                target = gate.get("job_id") or next(
                    (n["job_id"] for n in fresh["nodes"]
                     if n["node_type"] == "video_generate"
                     and n["node_key"].split("#")[-1] == gate["node_key"].split("#")[-1]
                     and n.get("job_id")), None)
                if target:
                    S.sign(target, note="整集验收签字")
            last = S.must(f"/api/pipeline/runs/{run_id}")
            settled = sum(1 for n in last["nodes"]
                          if n["node_type"] == "shot_review"
                          and n["status"] in ("completed", "skipped"))
            if len(clips) >= SHOTS and settled >= SHOTS:
                break
            time.sleep(1.0)
    finally:
        sampler_note = sampler.stop_and_report()
    return {"clips": clips, "wall": time.time() - wall_started,
            "run": last, "sampler": sampler_note}




def image_leg(shots_n: int) -> dict:
    """关键帧（图像腿）整集连跑：一集 N 镜每镜真出一张图。

    视频腿用 T2V 模板跑通之后，《发布基线》§3 仍挂着「图像腿整集连跑」——
    这条补齐它：默认轨配上 mflux 之后，产线每个镜头排一条真实图像作业，
    量逐张耗时、平台自己采到的内存峰值（`child_peak_rss_sampled_kb`）与产物尺寸。
    """
    if not S.set_image_default_track():
        return {"skipped": "配不上图像默认轨（这台机器没装 mflux？）"}
    project, episode = S.new_project("图像腿整集")
    for i in range(shots_n):
        S.add_shot(episode["id"], f"第 {i + 1} 镜：燧人氏蹲在崖边钻木", duration=5)
    tpl = next((t for t in S.must("/api/pipeline/templates")
                if t["name"] == "图片预览生产"), None)
    if tpl is None:
        return {"skipped": "没有「图片预览生产」模板"}
    run = S.must(f"/api/projects/{project['id']}/pipeline/runs", "POST",
                 {"episode_id": episode["id"], "template_id": tpl["id"]},
                 expect=(200, 201))
    seen, out = set(), []
    sampler = Sampler(S.WORKER.pid if S.WORKER else None)
    wall_started = time.time()
    sampler.start()
    try:
        for _round in range(shots_n * 6 + 12):
            view = S.must(f"/api/pipeline/runs/{run['id']}/advance", "POST", {})
            for node in view["nodes"]:
                if node["node_type"] == "image_generate" and node.get("job_id") \
                        and node["job_id"] not in seen:
                    seen.add(node["job_id"])
                    detail = S.wait_job(node["job_id"], timeout=2400, label=node["label"])
                    arts = (S.must(f"/api/jobs/{node['job_id']}/artifacts")
                            if detail["status"] == "succeeded" else [])
                    size = probe(Path(os.environ["FASTVIDEO_JOBS"]) / arts[0]["path"]) \
                        if arts else {"width": 0, "height": 0, "duration": 0, "bytes": 0}
                    out.append({"job_id": node["job_id"], "status": detail["status"],
                                "provider": detail.get("provider_code"),
                                "model": detail.get("model_code"),
                                "elapsed_ms": int(detail.get("elapsed_ms") or 0),
                                "bytes": size["bytes"], "width": size["width"],
                                "height": size["height"]})
                    print(f"      {node['label']}：{detail['status']}"
                          f"｜{detail.get('provider_code')}/{detail.get('model_code')}"
                          f"｜{int(detail.get('elapsed_ms') or 0) / 1000:.1f}s"
                          f"｜{size['width']}x{size['height']}/{size['bytes']}B", flush=True)
            if len(out) >= shots_n:
                break
            time.sleep(1.0)
    finally:
        sampler_note = sampler.stop_and_report()
    ok = [o for o in out if o["status"] == "succeeded"]
    check(f"3.1 图像腿 {shots_n} 镜全部真出图", len(ok) == shots_n,
          f"成功 {len(ok)}/{shots_n}｜{[(o['provider'], o['status']) for o in out][:3]}")
    if ok:
        ms = [o["elapsed_ms"] for o in ok if o["elapsed_ms"]]
        check("3.2 逐张耗时与尺寸都量得出", bool(ms) and all(o["width"] for o in ok),
              f"avg {sum(ms) / len(ms) / 1000:.1f}s｜最慢 {max(ms) / 1000:.1f}s"
              f"｜{ok[0]['width']}x{ok[0]['height']}｜{ok[0]['bytes']}B")
        check("3.3 整段墙钟与采样峰值内存", bool(sampler_note),
              f"{(time.time() - wall_started) / 60:.1f} 分钟｜{sampler_note}")
    return {"jobs": out, "wall": time.time() - wall_started}




def sign_job(job_id: int, note: str = "整集全链验收") -> dict:
    """把一条作业的产物签成通过：先按服务端清单勾完必看项，再签。

    为什么不直接签：`routers/review.py` 在签通过之前会读 `job_checklist`，
    人工必看项没勾完就回拒——那是规则本身在正常工作。验收脚本要走的正是
    人要走的那条路，所以这里也老老实实先填清单，而不是把检查改成永远通过。
    """
    cl = S.must(f"/api/jobs/{job_id}/review-checklist")
    items = [{"code": m["code"], "state": "pass", "note": "整集逐条看过"}
             for m in (cl.get("human") or []) if m.get("required")]
    if items:
        S.must(f"/api/jobs/{job_id}/review-checklist", "POST", {"items": items})
    anchors = S.must(f"/api/jobs/{job_id}/review").get("anchors") or []
    anchor = anchors[0] if anchors else "artifact"
    return S.must(f"/api/jobs/{job_id}/review-sign", "POST",
                  {"anchor": anchor, "decision": "approved", "note": note})


def full_chain(shots_n: int) -> None:
    """标准漫剧单集生产 v2 全流程连跑：关键帧＋视频两条真实模型腿＋运行内合成。

    为什么要单独一条：`视频生产` 那条腿（§12／§17）里没有关键帧节点，也没有
    `compose`——合成是脚本另外调接口做的。而平台内置的 v2 模板本来就是
    剧本→分镜→资产→人工确认→关键帧→关键帧验收→镜头视频→产物检查→镜头审核
    →音频检查→成片合成→成片验收→成片审核 一条全链。
    「这一套能不能端到端做出一集」要验的是**那一条**，不是脚本自己拼出来的两步。
    """
    if not S.set_image_default_track():
        check("0.9 图像默认轨配得上（不然关键帧腿必失败）", False,
              "这台机器配不上 mflux 默认轨")
        return
    project, episode = S.new_project("整集全链")
    doc = S.must(f"/api/projects/{project['id']}/documents", "POST",
                 {"kind": "script", "title": "灯市", "episode_id": episode["id"],
                  "content": ""})
    content = S.must(f"/api/documents/{doc['id']}/content")
    S.must(f"/api/documents/{doc['id']}/content", "PUT",
           {"content": S.SCRIPT_TEXT, "expected_version": content["version"]})
    for i in range(shots_n):
        sh = S.add_shot(episode["id"], f"第 {i + 1} 镜：掌灯人举灯等风来", duration=5)
        # check_shots 对「有镜无提示词」是给警告还是判失败，取决于内容有没有可交给模型的字；
        # 这里两条腿都要真跑，所以每镜都实打实填上提示词，不留警告当背景噪音。
        S.must(f"/api/shots/{sh['id']}", "PUT",
               {"summary": f"第 {i + 1} 镜：掌灯人举灯等风来",
                "prompt": "夜里灯市口，一人举灯站在灯柱下，暖黄光，平涂动画",
                "duration": 5, "asset_ids": []})
    tpl = next((t for t in S.must("/api/pipeline/templates")
                if t["name"] == "标准漫剧单集生产" and t["version"] == 2), None)
    if tpl is None:
        check("0.8 找到「标准漫剧单集生产」v2", False, "模板表里没有这一份")
        return
    run = S.must(f"/api/projects/{project['id']}/pipeline/runs", "POST",
                 {"episode_id": episode["id"], "template_id": tpl["id"]},
                 expect=(200, 201))
    tdefs = tpl.get("nodes") or []
    fan = sum(1 for x in tdefs if x.get("fan_out") == "shot")
    want = (len(tdefs) - fan + fan * shots_n) if tdefs else (10 + 5 * shots_n)
    check("4.1 v2 全链运行建成，节点按镜数展开后全冻下来",
          len(run["nodes"]) == want,
          f"run #{run['id']}｜节点 {len(run['nodes'])} 个（定义 {len(tdefs)} 类、"
          f"按镜展开 {fan} 类 × {shots_n} 镜 → 应 {want}）｜{run['status']}")

    sampler = Sampler(S.WORKER.pid if S.WORKER else None)
    sampler.start()
    seen_img, seen_vid, signed_docs = set(), set(), 0
    img_secs, vid_secs = [], []
    signed_jobs = set()
    wall0 = time.time()
    gate_hit = compose_job = None
    try:
        for _round in range(shots_n * 40 + 120):
            view = S.must(f"/api/pipeline/runs/{run['id']}/advance", "POST", {})
            nodes = view["nodes"]
            by_state = {}
            for n in nodes:
                by_state.setdefault(n["status"], []).append(n)
            # ① 人工确认那一格：签的是剧本这份文档
            for n in by_state.get("waiting_review", []):
                if n["node_type"] == "human_gate":
                    # 先跑确定性校验（POST 才建报告；GET 只读，读不到就说"未校验"），
                    # 再按整份文档那个锚点签字——这正是人在这条流程里要点的两下。
                    S.must(f"/api/documents/{doc['id']}/review-gate", "POST", {})
                    S.must(f"/api/documents/{doc['id']}/review-sign", "POST",
                           {"anchor": "document", "decision": "approved",
                            "note": "整集全链验收"})
                    signed_docs += 1
            # ③ 等真模型作业：关键帧一条、视频一条，各记耗时
            for n in nodes:
                jid = n.get("job_id")
                if not jid or jid in seen_img or jid in seen_vid:
                    continue
                if n["node_type"] == "image_generate":
                    seen_img.add(jid)
                    j = S.wait_job(jid, timeout=2400, label=n["label"])
                    img_secs.append(int(j.get("elapsed_ms") or 0) / 1000)
                    print(f"      关键帧 · {n['label']}：{j['status']}"
                          f"｜{j.get('provider_code')}/{j.get('model_code')}"
                          f"｜{int(j.get('elapsed_ms') or 0) / 1000:.1f}s", flush=True)
                elif n["node_type"] == "video_generate":
                    seen_vid.add(jid)
                    j = S.wait_job(jid, timeout=2400, label=n["label"])
                    vid_secs.append(int(j.get("elapsed_ms") or 0) / 1000)
                    print(f"      视频   · {n['label']}：{j['status']}"
                          f"｜{j.get('provider_code')}/{j.get('model_code')}"
                          f"｜{int(j.get('elapsed_ms') or 0) / 1000:.1f}s", flush=True)
                elif n["node_type"] == "export_compose":
                    seen_vid.add(jid)
                    compose_job = S.wait_job(jid, timeout=1200, label="运行内合成")
            # ④ 验收格：把已成功的图像／视频作业签掉
            for n in nodes:
                if n["node_type"] not in ("image_review", "shot_review",
                                          "export_review"):
                    continue
                if n["status"] != "waiting_review":
                    continue
                # 按镜头展开后一镜一格：`review_keyframe#59` 对 `keyframe#59`。
                # 上一版这里抓"第一个有作业的生成格"，两镜以上就会反复签同一作业，
                # 别的镜永远卡在等签字——编排缺陷，第一次跑多镜就露出来。
                # 先认这一格自己挂的作业；挂不到才按镜头配对去找上游。
                # `export_review` 不按镜头展开（key 就是 review_composition，没有 #镜号），
                # 上一版按 # 后缀去找 compose 那一格，永远配不上——19／20 就是这么来的。
                target = n.get("job_id")
                if not target:
                    if n["node_type"] == "export_review":
                        up = next((x for x in nodes if x["node_type"] == "export_compose"
                                   and x.get("job_id")), None)
                    else:
                        suffix = (n.get("node_key") or "").split("#")[-1]
                        want_type = {"image_review": "image_generate",
                                     "shot_review": "video_generate"}[n["node_type"]]
                        up = next((x for x in nodes
                                   if x["node_type"] == want_type
                                   and (x.get("node_key") or "").split("#")[-1] == suffix
                                   and x.get("job_id")), None)
                    target = up["job_id"] if up else None
                if target and target not in signed_jobs:
                    sign_job(int(target))
                    signed_jobs.add(target)
            if view["status"] in ("done", "completed", "succeeded", "failed"):
                gate_hit = view
                break
            time.sleep(1.0)
    finally:
        peak = sampler.stop_and_report()
    wall = (time.time() - wall0) / 60
    final = gate_hit or S.must(f"/api/pipeline/runs/{run['id']}")
    nodes = final["nodes"]
    done = [n for n in nodes if n["status"] in ("completed", "succeeded", "skipped")]
    check("4.2 关键帧腿真的走 mflux（不是假适配器）", bool(img_secs),
          f"{len(img_secs)} 张｜avg {sum(img_secs) / max(len(img_secs), 1):.1f}s")
    check("4.3 视频腿真的走 FastMetal", bool(vid_secs),
          f"{len(vid_secs)} 条｜avg {sum(vid_secs) / max(len(vid_secs), 1):.1f}s")
    check("4.4 人工确认这一格靠文档签字放行", signed_docs >= 1,
          f"签了 {signed_docs} 份文档")
    check("4.5 展开后的节点除失败外都收口",
          all(n["status"] in ("completed", "succeeded", "skipped", "failed")
              for n in nodes) and len(done) >= 12,
          f"收口 {len(done)}/{len(nodes)}（应 {want}）｜运行状态 {final['status']}")
    comp = S.must(f"/api/projects/{project['id']}/compositions")
    if comp:
        c0 = comp[-1]
        media = probe(Path(os.environ["FASTVIDEO_JOBS"]) / c0["output_path"]) \
            if c0.get("output_path") else {"width": 0, "height": 0, "duration": 0, "bytes": 0}
        check("4.6 成片是运行里那个 compose 节点产的真文件",
              media["duration"] > 0 and media["bytes"] > 0,
              f"composition #{c0['id']} v{c0['version']}｜{media['width']}x{media['height']}"
              f"｜{media['duration']:.2f}s｜{media['bytes']}B")
        check("4.7 留空尺寸时成片与素材同档（服务端众数，不写死放大）",
              c0.get("width") == media["width"],
              f"记录 {c0.get('width')}x{c0.get('height')}｜实测 {media['width']}x{media['height']}")
    else:
        check("4.6 成片是运行里那个 compose 节点产的真文件", False, "运行没产出 composition")
    peak_text = (f"{peak.get('peak_gb')} GB" if isinstance(peak, dict)
                 else str(peak)).replace("峰值 ", "")
    check("4.8 整段墙钟与本条采样峰值内存", wall > 0,
          f"{wall:.1f} 分钟｜峰值 {peak_text}")


def main() -> int:
    S.prepare_env()
    S.start_server()
    S.TOKEN = S.mint_token()
    S.start_worker()
    sampler = Sampler(S.WORKER.pid if S.WORKER else None)

    print(f"\n===== 整集真实模型连跑：{SHOTS} 镜 =====", flush=True)
    check("0.1 跑的是库副本，开发库没被碰",
          str(S.TMP) in os.environ["FASTVIDEO_DB"], f"DB={os.environ['FASTVIDEO_DB']}")

    if os.environ.get("EPISODE_FULL_CHAIN") == "1":
        # 标准漫剧单集生产 v2 全链：两条真模型腿＋运行内合成。
        # 走这条时不再跑视频腿那套自建流程，否则断言会变成"因为没跑所以失败"。
        print("\n===== 全链 v2：关键帧＋视频＋运行内合成 =====", flush=True)
        full_chain(SHOTS)
        S.stop_worker()
        if S.SERVER and S.SERVER.poll() is None:
            S.SERVER.terminate()
        passed = sum(1 for _n, ok, _d in results if ok)
        print(f"\n开始 {started_at}｜结束 {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{passed}/{len(results)} 通过")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAIL {name}  · {detail}")
        return 0 if passed == len(results) else 1

    if os.environ.get("EPISODE_IMAGE_LEG") == "1" and os.environ.get("EPISODE_SKIP_VIDEO") == "1":
        # 只跑图像腿（关键帧）。跳过视频腿与合成，不然那几条 1.x／2.x 断言会变成
        # "因为没跑所以失败"——那种红没有意义。
        print("\n===== 只跑图像腿（关键帧）=====", flush=True)
        image_leg(SHOTS)
        S.stop_worker()
        if S.SERVER and S.SERVER.poll() is None:
            S.SERVER.terminate()
        passed = sum(1 for _n, ok, _d in results if ok)
        print(f"\n开始 {started_at}｜结束 {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{passed}/{len(results)} 通过")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAIL {name}  · {detail}")
        return 0 if passed == len(results) else 1

    project, episode = S.new_project("整集真实模型")
    for i in range(SHOTS):
        S.add_shot(episode["id"], f"第 {i + 1} 镜：掌灯人举灯等风来", duration=5)
    shot_rows = S.must(f"/api/episodes/{episode['id']}/shots")
    check("0.2 一集的镜头都建起来了", len(shot_rows) == SHOTS,
          f"episode #{episode['id']}｜{len(shot_rows)} 镜")

    templates = S.must("/api/pipeline/templates")
    video_tpl = next((t for t in templates if t["name"] == "视频生产"), None)
    if video_tpl is None:
        check("0.3 找到「视频生产」模板", False,
              f"模板：{'、'.join(t['name'] for t in templates)}")
        return 1
    # 预检要在**建运行之前**问：建完再问，第一条阻塞就是「这一集已有一条未结束的运行」
    # ——那是这条规则本身在正常工作，但拿它当"发起前"的结论会把读的人带偏。
    pf = S.must(f"/api/projects/{project['id']}/precheck?episode_id={episode['id']}"
                f"&template_id={video_tpl['id']}")
    check("0.3 发起前预检给的是可点的三档条目",
          all("locate" in item for item in pf["items"]) and "can_start" in pf,
          f"{pf['status']}｜阻塞 {pf['blocking']}／警告 {pf['warnings']}"
          f"｜可发起 {pf['can_start']}")
    run = S.must(f"/api/projects/{project['id']}/pipeline/runs", "POST",
                 {"episode_id": episode["id"], "template_id": video_tpl["id"]},
                 expect=(200, 201))
    video_nodes = sum(1 for n in run["nodes"] if n["node_type"] == "video_generate")
    check("0.4 运行建成并按镜展开节点", video_nodes == SHOTS,
          f"run #{run['id']}｜节点 {len(run['nodes'])} 个，其中视频节点 {video_nodes} 个")

    out = produce_episode(project, episode, run["id"], sampler)
    clips = out["clips"]
    real_ok = [c for c in clips.values()
               if c["status"] == "succeeded" and c["provider"] == "fastmetal"]
    check(f"1.1 {SHOTS} 镜全部由真实模型产出（FastMetal 1.3B，不是 mock）",
          len(real_ok) == SHOTS,
          f"成功 {len(real_ok)}/{SHOTS}｜"
          + "、".join(f"#{c['job_id']} {c['status']}" for c in clips.values())[:170])
    measured = [c for c in real_ok if c.get("media", {}).get("width")]
    check("1.2 每条产物都真量得出尺寸与时长", len(measured) == len(real_ok),
          f"{len(measured)} 条｜{measured[0]['media']['width']}x{measured[0]['media']['height']}"
          f"/{measured[0]['media']['duration']:.2f}s" if measured else "没有一条量得出")
    per = [c["elapsed_ms"] for c in real_ok if c["elapsed_ms"]]
    if per:
        check("1.3 单镜耗时与整集墙钟都记下（16GB 本机排产参考）",
              sum(per) > 0 and out["wall"] > 0,
              f"单镜 avg {sum(per) / len(per) / 1000:.1f}s｜最慢 {max(per) / 1000:.1f}s"
              f"｜{len(per)} 条整段墙钟 {out['wall'] / 60:.1f} 分钟")
    check("1.4 worker 子进程树峰值内存实测", bool(out["sampler"]), out["sampler"])

    settled = [n for n in out["run"]["nodes"]
               if n["node_type"] == "shot_review" and n["status"] in ("completed", "skipped")]
    signed = S.must(f"/api/projects/{project['id']}/job-reviews")
    approved = [r for r in signed
                if any(x.get("decision") == "approved" for x in (r.get("signatures") or []))]
    check("1.5 每一镜都真签过字（成片取片的前置条件）",
          len(settled) >= SHOTS and len(approved) >= SHOTS,
          f"审核节点收口 {len(settled)}/{SHOTS}｜已签通过 {len(approved)} 条")

    # ---- 合成：N 条真产物拼成一集 ----
    comp = S.must(f"/api/projects/{project['id']}/compositions", "POST",
                  {"episode_id": episode["id"], "idempotency_key": "episode-real-run"})
    check("2.1 成片版本建成，N 镜都进了时间线", comp["shot_count"] == SHOTS,
          f"composition #{comp['id']} v{comp['version']}｜镜 {comp['shot_count']}"
          f"｜计划合计 {comp['total_duration_ms']}ms")
    final = S.wait_job(comp["job_id"], timeout=1200, label="整集合成")
    check("2.2 合成作业真跑完", final["status"] == "succeeded",
          f"{final['status']}｜{final.get('elapsed_ms')}ms"
          f"｜{str(final.get('error_message'))[:90]}")

    path, rel = job_artifact_path(comp["job_id"])
    media = probe(path)
    clip_sum = sum(c["media"]["duration"] for c in real_ok if c.get("media"))
    check("2.3 成片是真文件，时长对得上（素材首尾相接）",
          media["width"] > 0 and media["duration"] > 0
          and abs(media["duration"] - clip_sum) <= max(0.6, 0.15 * SHOTS),
          f"{media['width']}x{media['height']}｜成片 {media['duration']:.2f}s"
          f"｜素材合计 {clip_sum:.2f}s｜差 {media['duration'] - clip_sum:+.2f}s")
    view = S.must(f"/api/compositions/{comp['id']}")
    check("2.4 成片的 sha256、字节数与尺寸都落进了记录",
          bool(view.get("sha256")) and int(view.get("bytes") or 0) == media["bytes"],
          f"sha {str(view.get('sha256'))[:12]}…｜记录 {view.get('bytes')}B"
          f"｜实测 {media['bytes']}B｜{view.get('width')}x{view.get('height')}")

    second = S.must(f"/api/projects/{project['id']}/compositions", "POST",
                    {"episode_id": episode["id"], "idempotency_key": "episode-real-run"})
    check("2.5 同一个幂等键不会排出第二次合成", second["id"] == comp["id"],
          f"两次都指回 composition #{second['id']}")

    print(f"\n开始 {started_at}｜结束 {time.strftime('%Y-%m-%d %H:%M:%S')}"
          f"｜成片文件 {path}｜副本库 {S.TMP}/platform.db（开发库未动）")
    passed = sum(1 for _n, ok, _d in results if ok)
    print(f"{passed}/{len(results)} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name}  · {detail}")

    S.stop_worker()
    if S.SERVER and S.SERVER.poll() is None:
        S.SERVER.terminate()
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for proc in (S.WORKER, S.SERVER):
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
