"""隔离库验收：真实 SIGKILL/自然租约恢复，可选真实 FastMetal CLI；不触碰现役数据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "app/backend"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real", action="store_true", help="另跑本机 FastMetal 1.3B，真实模型需已安装"
    )
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="fv-runtime-acceptance-"))
    for key, value in {
        "DB": "platform.db",
        "FILES": "files",
        "JOBS": "jobs",
        "WORKSPACE": "workspace",
    }.items():
        os.environ["FASTVIDEO_" + key] = str(folder / value)
    os.environ.update(
        FASTVIDEO_ALLOW_MOCK="1",
        FASTVIDEO_WORKER_LEASE="2",
        FASTVIDEO_ADMIN_PASSWORD="isolated-runtime-acceptance-only",
    )
    home = Path.home() / "FastVideo"
    if home.is_dir():
        os.environ.setdefault("FASTVIDEO_FASTMETAL_HOME", str(home))
    sys.path.insert(0, str(BACKEND))
    from bootstrap import ensure_admin
    from fastapi.testclient import TestClient
    from main import app
    from models import JobSubmitIn
    from repositories.jobs import jobs
    from repositories.model_center import model_center
    from repositories.projects import projects
    from repositories.users import users
    from services import submit_job

    ensure_admin(verbose=False)
    admin = users.get_by_username("superadmin")
    assert admin is not None
    project = projects.create("隔离恢复验收", "本机临时数据", [], admin["id"])
    results: dict[str, object] = {
        "machine": platform.platform(),
        "checked_at": datetime.now().astimezone().isoformat(),
        "folder": str(folder),
        "target_benchmark": False,
    }
    processes: list[subprocess.Popen[bytes]] = []

    def start() -> subprocess.Popen[bytes]:
        with (folder / "worker-control.log").open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "worker.py", "--once"],
                cwd=BACKEND,
                env=dict(os.environ),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        processes.append(proc)
        return proc

    def wait_running(identifier: int, proc: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if jobs.get(identifier)["status"] == "running":
                assert proc.poll() is None
                return
            if proc.poll() is not None:
                raise AssertionError("worker 在进入 running 前退出；见控制日志")
            time.sleep(0.05)
        raise AssertionError("作业未进入 running")

    try:
        with TestClient(app):
            job = submit_job(
                JobSubmitIn(
                    project_id=project["id"],
                    capability="video",
                    execution_mode="local",
                    provider_code="mock",
                    model_code="mock-1",
                    prompt="隔离强杀验收",
                    params={"sleep": 30},
                ),
                admin["id"],
            )
            proc = start()
            wait_running(job["id"], proc)
            os.kill(proc.pid, signal.SIGKILL)
            assert proc.wait(timeout=10) == -signal.SIGKILL
            assert jobs.get(job["id"])["status"] == "running"
            time.sleep(3.1)  # 等真实租约过期，不改数据库时间戳。
            restarted = start()
            assert restarted.wait(timeout=20) == 0
            recovered = jobs.get(job["id"])
            assert (
                recovered["status"] == "failed"
                and recovered["error_code"] == "worker_lost"
            )
            assert len(jobs.list([project["id"]])) == 1, "恢复不得自动重试"
            retry = jobs.retry(recovered, admin["id"])  # 与人工重试接口相同的既有入口。
            # 用原参数快照完成重试，不缩短原任务来假装恢复通过。
            assert start().wait(timeout=60) == 0
            assert jobs.get(retry["id"])["status"] == "succeeded"
            results["KILL-01"] = {
                "passed": True,
                "signal": "SIGKILL",
                "lease": "natural expiry",
                "original": job["id"],
                "retry": retry["id"],
                "auto_retry": False,
            }
            print(
                "KILL-01 PASS: real worker SIGKILL -> natural expiry -> worker_lost -> manual retry succeeded",
                flush=True,
            )
            if args.real:
                import runtime

                checked = runtime.probe_fastmetal("fastmetal-1.3b-qad")
                assert checked.state == "ok", "真实 FastMetal 运行时未就绪"
                model_center.create(
                    {
                        "capability": "video",
                        "backend": "local",
                        "provider": "fastmetal",
                        "model_code": "fastmetal-1.3b-qad",
                        "enabled": True,
                    }
                )
                prompt = (
                    "A red paper lantern on a wooden table, still camera, warm light."
                )
                real = submit_job(
                    JobSubmitIn(
                        project_id=project["id"],
                        capability="video",
                        execution_mode="local",
                        provider_code="fastmetal",
                        model_code="fastmetal-1.3b-qad",
                        prompt=prompt,
                        params={
                            "width": 640,
                            "height": 352,
                            "frames": 17,
                            "fast_spatial": True,
                        },
                    ),
                    admin["id"],
                )
                began = time.monotonic()
                assert start().wait(timeout=1800) == 0
                real = jobs.get(real["id"])
                assert real["status"] == "succeeded", "真实作业失败；查看隔离目录日志"
                import jobfiles

                artifacts = jobfiles.artifacts(real["result_paths_json"])
                assert artifacts
                log = jobfiles.read_log(real["log_path"])
                assert prompt not in log
                assert str(home) not in log
                target = jobfiles.artifact_at(real["result_paths_json"], 0)
                assert target is not None
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_name,width,height,nb_frames:format=duration",
                        "-of",
                        "json",
                        str(target),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(target), "-f", "null", "-"],
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
                measured = json.loads(probe.stdout)
                assert int(measured["streams"][0]["nb_frames"]) == 17
                results["real_model"] = {
                    "provider": "fastmetal",
                    "model": "fastmetal-1.3b-qad",
                    "job": real["id"],
                    "elapsed_seconds": round(time.monotonic() - began, 2),
                    "bytes": target.stat().st_size,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "full_decode": True,
                    "probe": measured,
                    "log_redaction": True,
                }
                print(
                    "REAL CLI PASS: FastMetal -> worker -> actual video -> ffprobe + log redaction",
                    flush=True,
                )
    finally:
        for proc in processes:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
        (folder / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        print("Evidence:", folder, flush=True)


if __name__ == "__main__":
    main()
