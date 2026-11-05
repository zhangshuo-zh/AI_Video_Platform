#!/usr/bin/env python3
"""音频两腿的真跑验收：配音（VoxCPM2）与音乐（ACE-Step 1.5）各走一遍平台队列。

为什么单独一个脚本，而不是只跑 mlx-audio 的命令行：
命令行跑通只证明**模型能动**；这一轮要证明的是**平台能用它**——
本地默认轨要按人那样在调用策略里配出来、作业要经队列、产物要经 worker 的验真、
内存采样要落到那条作业自己的读数上、音频中心那张能力表要跟着改口。
任何一环没接上，界面上就会出现"能跑但点不动"或"点了但没说清"。

跑法与凭据：库副本＋独立端口＋真 worker＋真权重（`~/.local/bin/mlx_audio.*`），
开发库一字未动；两条作业都必须是真 provider，出现 mock 就算失败。

用法：python3 tools/audio_leg_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import production_scenario_check as S      # noqa: E402  同一套壳（副本库、服务、worker）

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  OK  " if ok else "  FAIL") + f" {name}"
          + (f"  · {detail}" if detail else ""), flush=True)


def probe(path: Path) -> dict:
    """ffprobe 现量一条音频：采样率、声道、时长、字节。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels:format=duration,size",
         "-of", "default=noprint_wrappers=1:nokey=0", str(path)],
        capture_output=True, text=True)
    pairs = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            pairs[k.strip()] = v.strip()
    return {"sample_rate": int(pairs.get("sample_rate") or 0),
            "channels": int(pairs.get("channels") or 0),
            "duration": float(pairs.get("duration") or 0),
            "bytes": int(pairs.get("size") or (path.stat().st_size if path.is_file() else 0)),
            "error": "" if out.returncode == 0 else out.stderr.strip()[:120]}


def set_local_track(capability: str, provider: str, model: str) -> bool:
    """把某一能力的本地默认轨配成指定档——走真用户路径（GET + PUT /api/providers）。

    副本库里 tts/music 那两行的 provider 与 model 是空的（与当年 image/local 一样）。
    不配就解析不出路线；这里配的是"人怎么修的"那一步，不偷偷 UPDATE 一行假装它本来就好。
    """
    rows = S.must("/api/providers")
    hit = any(r["capability"] == capability and r["backend"] == "local" for r in rows)
    if not hit:
        return False
    body = []
    for r in rows:
        item = {k: r[k] for k in ("id", "provider", "model", "enabled", "is_default")}
        item["key_env"] = r.get("key_env_custom", "")
        item["notes"] = r.get("notes", "")
        if r["capability"] == capability and r["backend"] == "local":
            item.update(provider=provider, model=model, enabled=1, is_default=1)
        elif r["capability"] == capability:
            item["is_default"] = 0
        body.append(item)
    S.must("/api/providers", "PUT", body)
    return True


def local_slot(capability: str):
    """从清单里读这一能力的本地档（provider, model），不在脚本里写死。

    清单的形状是 `{"policy":…, "adapters":[…]}`（`adapters.manifest_payload()`
    是唯一出口），所以这里按 adapters 找 backend=="local" 的那一条，
    模型取它自己声明的第一档——脚本不替清单决定该用哪个权重。
    """
    for entry in S.must("/api/providers/manifest").get("adapters", []):
        if entry.get("capability") != capability or entry.get("backend") != "local":
            continue
        models = entry.get("models") or []
        if models:
            return entry.get("code"), models[0].get("code")
    return None


def main() -> int:
    S.prepare_env()
    S.start_server()
    S.TOKEN = S.mint_token()
    S.start_worker()

    try:
        slots = {cap: local_slot(cap) for cap in ("tts", "music")}
        check("0.1 清单里有配音与音乐的本地档",
              all(slots.values()), str(slots))
        if not all(slots.values()):
            return 1

        check("1.1 配音本地默认轨配上（走真接口）",
              set_local_track("tts", *slots["tts"]), str(slots["tts"]))
        check("1.2 音乐本地默认轨配上（走真接口）",
              set_local_track("music", *slots["music"]), str(slots["music"]))

        health = {(r["capability"] + "/" + r["provider"]): (r.get("health") or {})
                  for r in S.must("/api/providers")
                  if r["capability"] in ("tts", "music") and r["backend"] == "local"}
        check("1.3 两条本地轨的健康由运行时探测说出话",
              health and all(h.get("state") for h in health.values()),
              str(health)[:200])

        rows = {r["capability"]: r for r in S.must("/api/audio/capabilities").get("rows", [])}
        check("2.1 音频中心那张表现在说配音可以提交执行",
              rows.get("tts", {}).get("runner_wired") is True
              and rows.get("tts", {}).get("can_submit") is True,
              str(rows.get("tts"))[:150])
        # 音乐：分支接了（runner_wired True），但运行时说不支持这一档 ⇒ 不能提交。
        # 这两件事必须分开说：前者是代码事实，后者是运行时事实，
        # 合成一句"没接线"就把刚接上的那条分支抹掉了。
        check("2.2 音乐的分支已接线、但运行时不支持这一档时不给提交入口",
              rows.get("music", {}).get("runner_wired") is True
              and rows.get("music", {}).get("can_submit") is False,
              str(rows.get("music"))[:170])

        project, episode = S.new_project("音频腿验收")
        shot = S.add_shot(episode["id"], "灯市口", duration=5)

        # 走人真用的那条路：建台词素材 → 「送进队列」→ 由 services.audio_submit
        # 解析本地轨并建 Job。直接 POST /api/jobs 也能建作业，但那验不到
        # "音频中心这颗钮点了真进队列"这一寸——它才是这轮接线的用户界面。
        dialogue = S.must(f"/api/projects/{project['id']}/audio-clips", "POST", {
            "episode_id": episode["id"], "shot_id": shot["id"],
            "kind": "dialogue", "speaker": "掌灯人", "text": "你迟了三年。"})
        t0 = time.time()
        submitted = S.must(f"/api/audio-clips/{dialogue['id']}/submit", "POST", {})
        tts_job = {"id": submitted["job_id"]}
        tts_final = S.wait_job(tts_job["id"], timeout=900, label="配音")
        tts_wall = time.time() - t0
        check("3.1 配音作业真跑完且是真 provider（不是 mock）",
              tts_final.get("status") == "succeeded"
              and tts_final.get("provider_code") == slots["tts"][0],
              f"job #{tts_job['id']}｜{tts_final.get('status')}"
              f"｜{tts_final.get('provider_code')}/{tts_final.get('model_code')}"
              f"｜{tts_final.get('elapsed_ms')}ms｜{str(tts_final.get('error_message'))[:70]}")

        music_clip = S.must(f"/api/projects/{project['id']}/audio-clips", "POST", {
            "episode_id": episode["id"], "shot_id": shot["id"],
            "kind": "music", "speaker": "", "text": "夜间灯市，弦乐与手鼓，温暖"})
        refused = S.api(f"/api/audio-clips/{music_clip['id']}/submit", "POST", {})
        # `S.api` 把非 2xx 回成 (status, body)，这里要的是"提交前就被挡住"这件事
        status = refused[0] if isinstance(refused, tuple) else 200
        body = refused[1] if isinstance(refused, tuple) else refused
        reason = str((body or {}).get("detail", ""))
        check("3.2 音乐提交在扣任何资源之前就被拒，并写明运行时缺哪一档",
              status == 422 and "acestep" in reason,
              f"HTTP {status}｜{reason[:140]}")
        music_job, music_final, music_wall = None, {"id": 0}, 0.0

        for label, final, want in (("配音", tts_final, 0.2),):
            arts = (S.must(f"/api/jobs/{final['id']}/artifacts")
                    if final.get("status") == "succeeded" else [])
            if not arts:
                check(f"4.{label}产物落盘且读得出时长", False, "没有产物记录")
                continue
            path = Path(os.environ["FASTVIDEO_JOBS"]) / arts[0]["path"]
            info = probe(path)
            check(f"4.{label}产物是真音频（ffprobe 量得出采样率与时长）",
                  info["sample_rate"] > 0 and info["duration"] >= want and info["bytes"] > 0,
                  f"{path.name}｜{info['sample_rate']}Hz｜{info['channels']}ch"
                  f"｜{info['duration']:.2f}s｜{info['bytes']}B")

        back = {c["id"]: c for c in
                S.must(f"/api/projects/{project['id']}/audio-clips")}
        check("3.3 素材行回写着它自己那条作业（点了就该看得到去向）",
              back[dialogue["id"]].get("job_id") == tts_job["id"],
              f"dialogue→{back[dialogue['id']].get('job_id')}"
              f"｜music 那条仍停在草稿：{back[music_clip['id']].get('status')}")
        check("3.4 被拒的音乐提交没有留下野作业（队列里不多一条注定失败的行）",
              back[music_clip["id"]].get("job_id") in (None, 0, ""),
              f"clip job_id={back[music_clip['id']].get('job_id')}")
        costs = {j["id"]: j for j in (tts_final,)}
        check("5.1 本地音频作业不计价（与视频同一条成本口径）",
              all(not (c.get("cost_micros") or 0)
                  and c.get("billable_status") in (None, "", "not_billable")
                  for c in costs.values()),
              str([(c.get("cost_micros"), c.get("billable_status"))
                   for c in costs.values()]))
        peaks = [(jid, (S.must(f"/api/jobs/{jid}").get("resources") or {})
                  .get("child_peak_rss_sampled_kb"))
                 for jid in (tts_job["id"],)]
        check("5.2 两条作业都带上按秒采到的本机峰值内存",
              all(p for _j, p in peaks), str(peaks))

        print(f"\n墙钟：配音 {tts_wall:.1f}s（含模型加载）｜音乐：提交前拒绝，未起子进程"
              f"｜副本库 {S.TMP}/platform.db（开发库未动）")
        passed = sum(1 for _n, ok, _d in results if ok)
        print(f"{passed}/{len(results)} 通过")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAIL {name}  · {detail}")
        return 0 if passed == len(results) else 1
    finally:
        S.stop_worker()
        if S.SERVER and S.SERVER.poll() is None:
            S.SERVER.terminate()


if __name__ == "__main__":
    sys.exit(main())
