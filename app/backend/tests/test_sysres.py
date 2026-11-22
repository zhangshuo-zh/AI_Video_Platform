"""资源浮窗的采样、并发刷新与只读权限契约。"""
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

import diskplan
import jobfiles
import sysres


@pytest.fixture(autouse=True)
def isolated_samples(monkeypatch):
    monkeypatch.setattr(sysres, "_HARDWARE", sysres._SampleCache(1.5))
    monkeypatch.setattr(sysres, "_SPACE", sysres._SampleCache(5))
    monkeypatch.setattr(sysres, "_LAST_CPU_SAMPLE", None)


def test_linux_cpu_and_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(sysres, "Path", lambda name: tmp_path / Path(name).name)
    source = tmp_path / "stat"
    source.write_text("cpu 20 0 0 80 0 0 0 0 10 0\n")
    assert sysres._sample_cpu_linux() is None
    source.write_text("cpu 40 0 0 100 0 0 0 0 30 0\n")
    assert sysres._sample_cpu_linux() == 50
    assert sysres._sample_cpu_linux() is None
    source.write_text("cpu 80 bad 20 10 40\n")
    assert sysres._sample_cpu_linux() is None
    (tmp_path / "meminfo").write_text("MemTotal: 100 kB\nMemAvailable: 25 kB\n"
                                      "SwapTotal: 8 kB\nSwapFree: 2 kB\nHugePages_Free: 12\n")
    mem, swap = sysres._sample_memory_linux()
    assert (mem["used_bytes"], swap["used_bytes"]) == (75 * 1024, 6 * 1024)
    (tmp_path / "meminfo").unlink()
    assert sysres._sample_memory_linux()[0]["total_bytes"] == 0


@pytest.mark.parametrize("token, expected", [("0.00M", 0), ("1.50G", 1536 * 1024**2)])
def test_mac_sampling(monkeypatch, token, expected):
    outputs = {"hw.memsize": str(8 * 16384),
               "vm_stat": "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                          "Pages active: 2.\nPages wired down: 1.\nPages occupied by compressor: 1.",
               "vm.swapusage": f"total = 2.00G used = {token} free = 0.00M",
               "%cpu": "%CPU\n40.0\ninvalid\n60.0\n"}
    def run(cmd, **kwargs):
        assert kwargs["env"]["LC_ALL"] == "C"
        return outputs[cmd[-1]]
    monkeypatch.setattr(subprocess, "check_output", run)
    monkeypatch.setattr(sysres.platform, "system", lambda: "Darwin")
    mem, swap = sysres._sample_memory_darwin()
    assert mem == {"total_bytes": 131072, "used_bytes": 65536, "percent": 50}
    assert swap["used_bytes"] == expected
    assert sysres._sample_cpu("Darwin", 4, 0) == 25


def test_degraded_readings(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "check_output",
                        Mock(side_effect=subprocess.TimeoutExpired("vm_stat", 1.5)))
    monkeypatch.setattr(jobfiles, "JOBS_DIR", tmp_path)
    (tmp_path / "run.log").write_bytes(b"log")
    monkeypatch.setattr(jobfiles, "volume_usage", lambda: None)
    monkeypatch.setattr(diskplan.job_repo, "gc_reference_snapshot",
                        Mock(side_effect=sqlite3.OperationalError("busy")))
    assert sysres._sample_memory_darwin()[0]["percent"] == 0
    disk, cache = sysres._sample_cache_and_disk()
    assert disk == dict(total_bytes=0, used_bytes=0, percent=0, free_bytes=0)
    assert cache == dict(total_bytes=3, candidate_count=0, reclaimable_bytes=0)


def test_cache_expiry_and_refresh(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(sysres.time, "monotonic", lambda: now[0])
    hardware = Mock(return_value={})
    space = Mock(return_value=({}, {}))
    monkeypatch.setattr(sysres, "_sample_hardware", hardware)
    monkeypatch.setattr(sysres, "_sample_cache_and_disk", space)
    for now[0] in (10, 11, 12, 15):
        sysres.snapshot()
    assert (hardware.call_count, space.call_count) == (3, 2)
    sysres.invalidate_cache()
    sysres.snapshot()
    assert (hardware.call_count, space.call_count) == (3, 3)


def test_gc_during_cold_scan_discards_old_result(monkeypatch):
    started, waiting, release = (threading.Event() for _ in range(3))
    wait = sysres._SPACE.condition.wait
    def observed_wait():
        waiting.set()
        return wait()
    monkeypatch.setattr(sysres._SPACE.condition, "wait", observed_wait)
    calls = []
    def scan():
        version = len(calls)
        calls.append(version)
        if version == 0:
            started.set()
            assert release.wait(3)
        return {}, {"total_bytes": version}
    monkeypatch.setattr(sysres, "_sample_hardware", dict)
    monkeypatch.setattr(sysres, "_sample_cache_and_disk", scan)
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(sysres.snapshot)
        assert started.wait(2)
        follower = pool.submit(sysres.snapshot)
        try:
            assert waiting.wait(2)
            sysres.invalidate_cache()
        finally:
            release.set()
        for task in (owner, follower):
            assert task.result(3)["cache"]["total_bytes"] == 1
    assert calls == [0, 1]


def test_stale_read_and_failed_refresh_recovery():
    cache = sysres._SampleCache(0)
    assert cache.get(lambda: 1) == 1
    started, release = threading.Event(), threading.Event()
    def fail():
        started.set()
        assert release.wait(3)
        raise OSError("temporarily unavailable")
    with ThreadPoolExecutor(1) as pool:
        owner = pool.submit(cache.get, fail)
        try:
            assert started.wait(2)
            assert cache.get(lambda: 99) == 1
        finally:
            release.set()
        with pytest.raises(OSError):
            owner.result(3)
    assert cache.get(lambda: 2) == 2


def test_resource_endpoint_and_gc_invalidation(as_role, client, monkeypatch, tmp_path):
    monkeypatch.setattr(jobfiles, "JOBS_DIR", tmp_path)
    path = "/api/system/resources"
    admin, member = as_role("admin"), as_role("viewer")
    assert client.get(path).status_code == 401
    file = tmp_path / "private-user-name.log"
    file.write_bytes(b"abc")
    for caller, permitted in [(admin, True), (member, False)]:
        res = caller.get(path)
        assert res.status_code == 200
        body = res.json()
        assert set(body) == {"cpu", "memory", "swap", "disk", "cache", "platform", "observed_at"}
        assert body["cache"] == dict(total_bytes=3, reclaimable_bytes=0,
                                     candidate_count=0, can_clear=permitted)
        assert body["platform"] in {"Darwin", "Linux", "Unknown"}
        for field in ("cpu", "memory", "swap", "disk"):
            assert all(type(v) in (float, int) for v in body[field].values())
        assert str(tmp_path) not in res.text and file.name not in res.text
    file.write_bytes(b"abcdef")
    assert member.post("/api/system/gc-execute", json={}).status_code == 403
    assert admin.post("/api/system/gc-execute", json={"job_ids": []}).status_code == 200
    assert member.get(path).json()["cache"]["total_bytes"] == 6
