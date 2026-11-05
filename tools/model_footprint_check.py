#!/usr/bin/env python3
"""本机真实模型的耗时与内存峰值实测（《发布基线》§3 那两行开口的取证工具）。

为什么要专门量这一件事：文档里一直写着「1024 档内存峰值未采」「目标机基准未测」，
而**排产要的就是这两个数**——16GB 这台机器同时能排几条、1024 档要不要留到目标机跑，
取决于每条作业真正吃掉多少内存，不是取决于模型名或网上说的数。

两条纪律：
1. **只量真实进程**。走的是平台的 Job 队列与真子进程（FastMetal / mflux），
   不是 CLI 手工跑一遍再抄进文档——那样量不到 worker 侧的封装与超时行为。
2. **峰值沿 worker 的子孙进程往下找**。全机最大那条可能是浏览器或聚焦索引，
   跟这次生产没关系；把它的数字写进验收记录就是编数据。

用法：
    python3 tools/model_footprint_check.py                    # 视频 + 图像两条都量
    python3 tools/model_footprint_check.py --only image       # 只量一条
    IMAGE_SIZE=1536x864 python3 tools/model_footprint_check.py
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import production_scenario_check as S     # noqa: E402  同一套壳：库副本、服务、真 worker

S.TMP = Path(os.environ.get("FOOTPRINT_TMP", "/tmp/footprint_check"))
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "1024x1024")

started_at = time.strftime("%Y-%m-%d %H:%M:%S")
rows: list[dict] = []


def rss_rows():
    out = subprocess.run(["ps", "-eo", "pid,ppid,rss,vsz,comm"],
                         capture_output=True, text=True).stdout
    parsed = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            parsed.append((int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), parts[4]))
        except ValueError:
            continue
    return parsed


class Sampler:
    """沿 ppid 链把 worker 的子孙进程都算进来，按 `every` 秒采一次峰值 RSS。"""

    def __init__(self, root_pid: int | None, every: float = 1.0):
        self.root = root_pid
        self.every = every
        self.stop = threading.Event()
        self.peak_kb = 0
        self.peak_cmd = ""
        self.peak_vsz_kb = 0
        self.samples = 0
        self.busiest = 0          # 同一瞬间最多有几个子孙在跑（串行／并行的现场证据）
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop.is_set():
            rows = rss_rows()
            if self.root:
                kids = {self.root}
                changed = True
                while changed:
                    changed = False
                    for pid, ppid, _rss, _vsz, _cmd in rows:
                        if ppid in kids and pid not in kids:
                            kids.add(pid)
                            changed = True
                live = [(rss, cmd, vsz) for pid, _ppid, rss, vsz, cmd in rows
                        if pid in kids and pid != self.root]
                self.busiest = max(self.busiest, len(live))
                for rss, cmd, vsz in live:
                    if rss > self.peak_kb:
                        self.peak_kb, self.peak_cmd = rss, cmd
                    self.peak_vsz_kb = max(self.peak_vsz_kb, vsz)
                self.samples += 1
            self.stop.wait(self.every)

    def start(self):
        self.thread.start()

    def finish(self) -> dict:
        self.stop.set()
        self.thread.join(timeout=6)
        return {"peak_gb": round(self.peak_kb / 1024 ** 2, 2),
                "peak_cmd": self.peak_cmd,
                "peak_vsz_gb": round(self.peak_vsz_kb / 1024 ** 2, 2),
                "samples": self.samples, "concurrent_children": self.busiest}


def machine_total_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
    try:
        return round(int(out.stdout.strip()) / 1024 ** 3, 1)
    except ValueError:
        return 0.0


def find_slot(capability: str):
    """取**配置里这一档真正会用到的那条槽位**（默认轨优先），而不是清单里的第一条。

    原来这里按清单顺序取第一个匹配节点，结果视频档量到了 `mlx-video/wan2.2-i2v-a14b`
    ——而配置槽位是 `fastmetal/fastmetal-1.3b-qad`，提交直接被 422 拒（那句拒得对：
    指定的与配的必须一致）。要量的是"这条轨道真跑一次吃多少"，
    所以读的是 `/api/providers` 的行（与 `resolve_provider()` 同一份依据），不是清单顺序。
    """
    rows = [r for r in S.must("/api/providers")
            if r.get("capability") == capability and r.get("backend") == "local"
            and r.get("enabled") and r.get("provider") and r.get("model")]
    for row in rows:
        if row.get("is_default"):
            return row["provider"], row["model"]
    return (rows[0]["provider"], rows[0]["model"]) if rows else None


def measure(kind: str, capability: str, params: dict, label: str, timeout: int) -> dict:
    """排一条真实作业，采它的墙钟、内存峰值与产物尺寸。"""
    project, episode = S.new_project(f"footprint-{kind}")
    shot = S.add_shot(episode["id"], label, duration=5)
    if capability == "image":
        # 图像这一腿在副本库里默认轨是空的（六场景里那条真失败就是它），
        # 所以复用同一个工具函数把人怎么修的那一步走一遍——量的正是"配上之后真跑一次吃多少"。
        if not S.set_image_default_track():
            return {"kind": kind, "skipped": "配不上图像默认轨（这台机器没装 mflux？）"}
    slot = find_slot(capability)
    if slot is None:
        return {"kind": kind, "skipped": f"清单里没有 {capability}/local 这一档"}
    provider, model = slot
    job = S.submit(capability, params=params, project_id=project["id"],
                   episode_id=episode["id"], shot_id=shot["id"],
                   prompt=label, mode="local", provider=provider, model=model)
    wall_started = time.time()
    sampler = Sampler(S.WORKER.pid if S.WORKER else None)
    sampler.start()
    detail = S.wait_job(job["id"], timeout=timeout, label=label)
    foot = sampler.finish()
    out = {"kind": kind, "capability": capability, "job_id": job["id"],
           "provider": detail.get("provider_code"), "model": detail.get("model_code"),
           "status": detail["status"], "elapsed_ms": int(detail.get("elapsed_ms") or 0),
           "wall_s": round(time.time() - wall_started, 1),
           "error": detail.get("error_message") or "", "params": params}
    out.update(foot)
    if detail["status"] == "succeeded":
        arts = S.must(f"/api/jobs/{job['id']}/artifacts")
        path = Path(os.environ["FASTVIDEO_JOBS"]) / arts[0]["path"]
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration,size",
             "-of", "default=noprint_wrappers=1:nokey=0", str(path)],
            capture_output=True, text=True)
        out["artifact"] = arts[0]["path"]
        out["media"] = probe.stdout.strip().replace("\n", " ")
        out["bytes"] = arts[0]["size"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["video", "image"], help="只量一条腿")
    args = parser.parse_args()

    S.prepare_env()
    S.start_server()
    S.TOKEN = S.mint_token()
    S.start_worker()

    total_gb = machine_total_gb()
    print(f"\n===== 本机模型占用实测（物理内存 {total_gb} GB，"
          f"库副本 {S.TMP}，开发库未动）=====", flush=True)
    w, h = (int(x) for x in IMAGE_SIZE.lower().split("x"))
    if args.only != "image":
        rows.append(measure("视频 640×352×81（预览档）", "video",
                            {"width": 640, "height": 352, "frames": 81,
                             "fast_spatial": True}, "崖顶黄昏，掌灯人举灯", 2400))
    if args.only != "video":
        rows.append(measure(f"图像 {w}×{h}", "image",
                            {"width": w, "height": h, "steps": 4}, "燧人氏蹲在崖边钻木", 1800))

    for row in rows:
        print(f"\n{row['kind']}：{row.get('status', '跳过')}"
              f"{'｜' + row['skipped'] if row.get('skipped') else ''}", flush=True)
        if row.get("skipped"):
            continue
        print(f"  轨道 {row['provider']}/{row['model']}｜参数 {row['params']}"
              f"｜作业 #{row['job_id']}")
        print(f"  执行器报的耗时 {row['elapsed_ms'] / 1000:.1f}s"
              f"｜排队到终态墙钟 {row['wall_s']}s")
        print(f"  峰值 RSS {row['peak_gb']} GB（{row['peak_cmd']}）"
              f"｜占物理内存 {round(row['peak_gb'] / total_gb * 100, 1) if total_gb else '?'}%"
              f"｜峰值 VSZ {row['peak_vsz_gb']} GB"
              f"｜采样 {row['samples']} 次｜同时在跑的子进程最多 {row['concurrent_children']} 个")
        if row.get("media"):
            print(f"  产物 {row['artifact']}｜{row['media']}｜{row['bytes']}B")
            if row.get("error"):
                print(f"  原话 {row['error'][:160]}")

    S.stop_worker()
    if S.SERVER and S.SERVER.poll() is None:
        S.SERVER.terminate()
    print(f"\n开始 {started_at}｜结束 {time.strftime('%Y-%m-%d %H:%M:%S')}"
          f"｜结果请连《发布基线》§3 一起看（这里不给推算值）")
    failed = [r for r in rows if r.get("status") not in ("succeeded",) and not r.get("skipped")]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for proc in (S.WORKER, S.SERVER):
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
