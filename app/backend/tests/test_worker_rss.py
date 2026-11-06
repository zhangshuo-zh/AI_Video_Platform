"""worker 的每条作业内存峰值采样（`_RssWatch`／`descendant_peak`）。

为什么要专门测这一件事：`resources_json.child_max_rss_kb` 取自
`getrusage(RUSAGE_CHILDREN)`，那是 **worker 生命周期内的最大值**，不是这一条的。
本机 2026-11-05 实测撞出来的形状是：跑完一条 8.75GB 的 FastMetal 视频作业之后，
紧接着的一条 1024×1024 mflux 图像作业也被记成 8.75GB，而它自己那棵子进程树
按秒采到的是 3.13GB。排产若按那个数判断「图像作业也要 8.75GB」就会少排一倍活。

四条判据：树要走到底（模型在孙子那一层）、无关进程不能算进来、
没有读数就不给数、参考并行的算式要能复核。
"""
import os
import subprocess
import sys

import production
import worker


def test_峰值只算这一棵子进程树的():
    rows = [
        (100, 1, 50_000),      # worker 自己
        (101, 100, 80_000),    # 直接子进程（壳）
        (102, 101, 900_000),   # 孙子：模型真正吃的那一层
        (200, 7, 9_999_999),   # 无关进程，比整机谁都大——不能算进来
    ]
    assert worker.descendant_peak(rows, 100) == 900_000


def test_树走不到底时不误报为零():
    """`ps` 的父子关系可能只有一层（子进程自己就是模型进程）。"""
    rows = [(100, 1, 50_000), (101, 100, 120_000)]
    assert worker.descendant_peak(rows, 100) == 120_000
    assert worker.descendant_peak([], 100) == 0


def test_采样线程量得出这条作业自己的峰值():
    """起一个真占内存的子进程，采到的峰值必须落在合理量级上。

    断言写成一个**下限**而不是等号：RSS 依机器与 Python 版本浮动，
    钉死一个数就是一条会在别人机器上乱红的用例。
    """
    # 采样本就要靠系统的 `ps`。容器/沙箱里它可能直接被禁（本机实测
    # PermissionError），那时 `_ps_rows()` 会诚实地返回空列表——用例要明说
    # "这一档没取证"，而不是拿一个 0 去撞断言、把环境限制演成代码回归。
    if not worker._ps_rows() or os.environ.get("FASTVIDEO_TEST_SKIP_ENV") == "1":
        import pytest
        pytest.skip("这台机器不给跑 ps；内存采样这一档无法取证")
    child = subprocess.Popen(
        [sys.executable, "-c",
         "b = bytearray(128 * 1024 * 1024); import time; time.sleep(2.0); print(len(b))"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    watch = worker._RssWatch(child.pid, every=0.2).start()
    try:
        child.wait(timeout=15)
    finally:
        peak_kb = watch.stop()
    assert child.returncode == 0
    assert peak_kb > 100_000, f"采到的峰值 {peak_kb} KB，低于子进程实际占用的量级"


def test_没有采样读数时不编一个峰值():
    """老的作业行没有这一栏——统计必须说「没读数」，不能拿生命周期最大值顶上。"""
    out = production._memory_readout([
        {"capability": "video", "peak_kb": None, "n": 0},
        {"capability": "image", "peak_kb": 0, "n": 0}])
    assert out["by_capability"] == {}
    assert out["parallel_reference"] == {}
    assert "这一条作业" in out["note"]


def test_参考并行的算式跟着峰值走():
    """这一栏是给人复核的除法，不是结论：算式必须与数字一起出现。"""
    total = production._machine_total_gb()
    if not total or os.environ.get("FASTVIDEO_TEST_SKIP_ENV") == "1":
        import pytest
        pytest.skip("这台机器问不到物理内存总量，参考值本就不该给")
    peak_gb = 8.75
    kb = int(peak_gb * 1024 ** 2)
    out = production._memory_readout([{"capability": "video", "peak_kb": kb, "n": 3}])
    item = out["by_capability"]["video"]
    assert item["peak_gb"] == 8.75 and item["sampled_jobs"] == 3
    ref = out["parallel_reference"]["video"]
    assert ref["parallel_reference"] == max(1, int(total * 0.8 // peak_gb))
    assert "0.8" in ref["formula"] and "8.75" in ref["formula"]


def test_统计走的是同一条采样口径(as_role, fresh_project):
    """界面那一栏的数字必须来自 `resources_json` 里那条作业自己的采样值。"""
    import jobfiles
    from repositories.content import episodes
    from repositories.jobs import jobs as job_repo

    c = as_role("editor")
    pid = fresh_project("editor")
    eid = episodes.create(pid, "第 1 集")["id"]
    row = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": None,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "fastmetal", "model_code": "fastmetal-1.3b-qad",
        "adapter_version": "test", "prompt_snapshot": "内存口径", "params": {},
        "input_refs": []})
    assert job_repo.claim_next("rss-worker", 60)["id"] == row["id"]
    _logs, outputs = jobfiles.ensure_dirs(row["id"])
    target = outputs / "out.mp4"
    target.write_bytes(b"x")
    job_repo.succeed(row["id"], "rss-worker", "", [jobfiles.relative(target)], 30_000,
                     resources={"child_peak_rss_sampled_kb": int(4.5 * 1024 ** 2)})
    stats = c.get(f"/api/projects/{pid}/production?episode_id={eid}").json()["stats"]
    assert stats["memory"]["by_capability"]["video"]["peak_gb"] == 4.5
    assert stats["memory"]["by_capability"]["video"]["sampled_jobs"] == 1
