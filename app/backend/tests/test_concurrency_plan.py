"""并发上限的网页配置与内存风险告警（0047）。

这一组测的是**判断本身**，不是"接口能返回 200"：

* 估算必须按**这台机器**的实测样本走——喂固定样本就得到固定结论，
  这样"改成永远 safe"这种退化能被抓住（见变异验红那两条）。
* `mock`／`ffmpeg` 的样本必须排除。它们是真实踩过的坑：mock 的假适配器
  只烧 22 MB，混进 video 样本会把 2038 MB 的真实峰值抹到几百 MB，
  估出来的安全上限直接错一个数量级。
* `NULL ≠ 1`：库里没设过与"人设成 1"必须是两件事，否则分不清
  "跟随命令行"和"人明确要求 1"。
* 危险档（over）不勾确认必须被拒——`claim_next` 只认数字，
  存进去下一轮就真会去领那么多条。
"""
from __future__ import annotations

import os
import tempfile

import pytest


def _row(capability, provider_code, rss_kb):
    """造一条作业样本行，形状与 `job` 表取出来的三列一致。"""
    import json
    return {"capability": capability, "provider_code": provider_code,
            "resources_json": json.dumps({
                "child_peak_rss_sampled_kb": rss_kb})}


class TestEstimate:
    """估算口径。"""

    def test_mock_和_ffmpeg_样本要被排除(self):
        """假适配器与转码的读数不是模型占用，混进样本会把峰值抹平。

        这条是真踩到的：本机 video 的真实峰值 2038 MB，而 mock 只写 22 MB。
        若不过滤，7 条 mock 会把 video 的平均拉到几十 MB，
        于是"global=8 也安全"——正是最危险的那种假结论。
        """
        import concurrency_plan as cp
        rows = [_row("video", "mock", 22000),
                _row("video", "fastmetal", 2038 * 1024),
                _row("video", "ffmpeg", 30000)]
        samples = cp.samples_by_class(rows)
        assert len(samples["video"]) == 1, f"只该留下真模型的 1 条，实得 {samples['video']}"
        assert round(samples["video"][0] / 1024 ** 2) == 2038

    def test_没有样本时说_unknown_而不是_safe(self):
        """没跑过作业＝估不出来，不能默认说"安全"。

        拿"没样本"当"安全"是最坏的失败方式：它恰好在你最不确定的时候
        给出最放心的结论。
        """
        import concurrency_plan as cp
        out = cp.assess(limits={"global": 8, "per": {}}, rows=[])
        assert out["verdict"] == "unknown"
        assert "估不出" in out["sentence"] or "没跑过" in out["sentence"]

    def test_超出可用内存判_over_并说清数字(self):
        """16 GB 机器、4 GB 系统常驻、每类峰值照实测 → 8 条必然溢出。"""
        import concurrency_plan as cp
        rows = [_row("video", "fastmetal", 2038 * 1024),
                _row("image", "mflux", 1448 * 1024),
                _row("audio", "voxcpm2", 422 * 1024),
                _row("text", "deepseek", 28 * 1024)]
        out = cp.assess(limits={"global": 8, "per": {}}, rows=rows,
                        total_mb=16384)
        assert out["verdict"] == "over"
        # 那句话必须带数字，不能只说"有风险"——人要能自己复核
        assert str(out["estimated_need_mb"]) != ""
        assert "内存溢出风险" in out["sentence"]
        assert "GB" in out["sentence"]

    def test_同样的样本_上限小就不溢出(self):
        """同一台机器，上限 1 应当是 safe——证明结论真的跟着上限走，
        不是恒定值（否则上面那条 over 也可能是写死的）。"""
        import concurrency_plan as cp
        rows = [_row("video", "fastmetal", 2038 * 1024),
                _row("image", "mflux", 1448 * 1024),
                _row("audio", "voxcpm2", 422 * 1024),
                _row("text", "deepseek", 28 * 1024)]
        out = cp.assess(limits={"global": 1, "per": {}}, rows=rows,
                        total_mb=16384)
        assert out["verdict"] == "safe"

    def test_读不到总内存时说_unknown(self):
        """`total_mb=0` 表示量不出来；此时不许拍一个结论。
        宁可说"估不出"，也不要拿别的机器或文档里的数字顶。"""
        import concurrency_plan as cp
        rows = [_row("video", "fastmetal", 2038 * 1024)]
        out = cp.assess(limits={"global": 1, "per": {}}, rows=rows, total_mb=0)
        assert out["verdict"] == "unknown"
        assert "读不到" in out["sentence"]

    def test_只有_over_才算危险档(self):
        """tight 只是提醒，不拦。把提醒也做成拦截，人会一路点确认，
        真危险时那个框就没人看了。"""
        import concurrency_plan as cp
        assert cp.needs_acknowledge({"verdict": "over"}) is True
        assert cp.needs_acknowledge({"verdict": "tight"}) is False
        assert cp.needs_acknowledge({"verdict": "safe"}) is False
        assert cp.needs_acknowledge({}) is False


class TestStorage:
    """`worker_limit` 那一行的语义。"""

    def test_null_不等于_1(self, tmp_path, monkeypatch):
        """没设过 → None；人设成 1 → 1。这两件事必须能分开。

        混成一个值以后，`./run.sh worker --max-global 2`（命令行）与
        "网页上设了 2"在库里长得一样，排查"为什么实际跑了 2 条"时无从下手。
        """
        monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
        from migrate import migrate
        migrate(verbose=False)
        from repositories import worker_limit
        worker_limit.forget()

        assert worker_limit.get() is None
        assert worker_limit.limits_or_none() is None

        worker_limit.save(global_max=1, per={})
        got = worker_limit.get()
        assert got["global"] == 1
        assert got["global"] is not None

    def test_只设每类没设全局_不回一个_None_全局(self, tmp_path, monkeypatch):
        """`limits_or_none()` 在"只设了 per"时不该塞 `global: None` 进去——
        那会让 `claim_next` 拿 None 当上限。它应当**不出现这个键**，
        由调用方用默认值。"""
        monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
        from migrate import migrate
        migrate(verbose=False)
        from repositories import worker_limit
        worker_limit.save(global_max=None, per={"video": 2})
        out = worker_limit.limits_or_none()
        assert out is not None
        assert "global" not in out, f"不该出现 global 键，实得 {out}"
        assert out["per"] == {"video": 2}


class TestPriority:
    """命令行 > 网页 > 默认。"""

    def test_命令行显式给的不会被网页旧值盖掉(self, tmp_path, monkeypatch):
        """这是硬规矩：`./run.sh worker --max-global 2` 是 operator 此刻
        对着这台机器下的指令，不该被一周前在网页上设的旧值悄悄盖掉。"""
        monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
        from migrate import migrate
        migrate(verbose=False)
        from repositories import worker_limit
        worker_limit.save(global_max=8, per={})
        import worker
        out = worker._limits_for_round(
            {"global": 2, "per": {}},
            {"global": True, "per": set()})
        assert out["global"] == 2, f"命令行给了 2，实得 {out['global']}"

    def test_命令行没给就用网页的值(self, tmp_path, monkeypatch):
        """这正是"能在网页上改"这件事本身：不写参数时网页设置就该生效。"""
        monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
        from migrate import migrate
        migrate(verbose=False)
        from repositories import worker_limit
        worker_limit.save(global_max=3, per={"video": 2})
        import worker
        out = worker._limits_for_round(
            {"global": 1, "per": {}},
            {"global": False, "per": set()})
        assert out["global"] == 3
        assert out["per"]["video"] == 2

    def test_上限字典必须能直接_json_序列化(self, tmp_path, monkeypatch):
        """`_limits_for_round` 的返回值会被心跳 `json.dumps` 写进
        `worker_node.limits_json`——里面只要有一个 set 就当场
        "Object of type set is not JSON serializable"，心跳炸掉。

        这是真踩到的：第一版把 `explicit`（含 set）塞进 limits 一起返回，
        `./run.sh worker --once` 直接抛 TypeError。所以这条钉两件事：
        返回体只有 global／per，且真的能 dumps。
        """
        monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
        from migrate import migrate
        migrate(verbose=False)
        from repositories import worker_limit
        worker_limit.save(global_max=5, per={"video": 2})
        import json
        import worker
        out = worker._limits_for_round(
            {"global": 1, "per": {}},
            {"global": False, "per": set()})
        assert set(out) == {"global", "per"}, f"多余的键会被写进库：{sorted(out)}"
        json.dumps(out)          # 不抛就算过

    def test_每类可以各管各的(self, tmp_path, monkeypatch):
        """网页设 video=2、命令行给 image=3 → 两个都要在。
        不该"有一边给了就整份覆盖另一份"。"""
        monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
        from migrate import migrate
        migrate(verbose=False)
        from repositories import worker_limit
        worker_limit.save(global_max=4, per={"video": 2})
        import worker
        out = worker._limits_for_round(
            {"global": 1, "per": {"image": 3}},
            {"global": False, "per": {"image"}})
        assert out["per"]["image"] == 3, "命令行显式给的 image 必须保住"
        assert out["per"]["video"] == 2, "网页设的 video 也要在"


class TestService:
    """服务层的拦阻与告警。"""

    def _prepare(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FASTVIDEO_DB", str(tmp_path / "t.db"))
        from migrate import migrate
        migrate(verbose=False)
        from repositories import worker_limit
        worker_limit.forget()
        return worker_limit

    def test_危险档不勾确认要被拒(self, tmp_path, monkeypatch):
        """`claim_next` 只认数字——存进去下一轮真会去领那么多条。
        所以这里必须在写库之前拦住，而不是存下再提醒。"""
        wl = self._prepare(tmp_path, monkeypatch)
        import services.jobs as svc
        from models import WorkerLimitIn
        # 先塞一份"内存很大"的样本，制造 over
        import json, sqlite3
        from db import get_conn
        with get_conn() as conn:
            for _ in range(3):
                conn.execute(
                    """INSERT INTO job (project_id, capability, kind, execution_mode,
                           provider_code, model_code, status, resources_json,
                           queued_at, created_at, updated_at)
                       VALUES (NULL,'video','video_local','local','fastmetal','m',
                               'succeeded',?,datetime('now'),datetime('now'),datetime('now'))""",
                    (json.dumps({"child_peak_rss_sampled_kb": 8 * 1024 * 1024}),))

        with pytest.raises(Exception) as exc:
            svc.set_concurrency(WorkerLimitIn(global_max=8))
        assert "内存溢出风险" in str(exc.value)
        # 必须**没写进库**：拦在保存之前，不是存完再报警
        assert wl.get() is None, "被拒之后库里不该留下值"

    def test_勾了确认就能存(self, tmp_path, monkeypatch):
        """确认框不是走过场：勾了就该真的存下来，否则人没法做这个决定。"""
        wl = self._prepare(tmp_path, monkeypatch)
        import services.jobs as svc
        from models import WorkerLimitIn
        import json
        from db import get_conn
        with get_conn() as conn:
            for _ in range(3):
                conn.execute(
                    """INSERT INTO job (project_id, capability, kind, execution_mode,
                           provider_code, model_code, status, resources_json,
                           queued_at, created_at, updated_at)
                       VALUES (NULL,'video','video_local','local','fastmetal','m',
                               'succeeded',?,datetime('now'),datetime('now'),datetime('now'))""",
                    (json.dumps({"child_peak_rss_sampled_kb": 8 * 1024 * 1024}),))
        out = svc.set_concurrency(
            WorkerLimitIn(global_max=8, acknowledge_risk=True))
        assert out["assessment"]["verdict"] == "over"
        assert wl.get()["global"] == 8

    def test_不认识的资源类要拒(self, tmp_path, monkeypatch):
        """拼错一个类名不该被静默忽略——那样人以为设好了，实际没设。"""
        self._prepare(tmp_path, monkeypatch)
        import services.jobs as svc
        from models import WorkerLimitIn
        with pytest.raises(Exception) as exc:
            svc.set_concurrency(WorkerLimitIn(global_max=1, per={"vidoe": 2}))
        assert "vidoe" in str(exc.value)

    def test_over_必须进_alerts_而且是_bad(self, tmp_path, monkeypatch):
        """设置卡平时没人打开，而内存溢出是"下一次领活就发生"的事。
        所以它必须进顶栏那个每人都会拉到的 alerts。"""
        wl = self._prepare(tmp_path, monkeypatch)
        import services.jobs as svc
        from models import WorkerLimitIn
        import json
        from db import get_conn
        with get_conn() as conn:
            for _ in range(3):
                conn.execute(
                    """INSERT INTO job (project_id, capability, kind, execution_mode,
                           provider_code, model_code, status, resources_json,
                           queued_at, created_at, updated_at)
                       VALUES (NULL,'video','video_local','local','fastmetal','m',
                               'succeeded',?,datetime('now'),datetime('now'),datetime('now'))""",
                    (json.dumps({"child_peak_rss_sampled_kb": 8 * 1024 * 1024}),))
        svc.set_concurrency(WorkerLimitIn(global_max=8, acknowledge_risk=True))
        status = svc.worker_status(None)
        codes = [a["code"] for a in status["alerts"]]
        assert "concurrency_over_memory" in codes, f"告警里没有这一条：{codes}"
        over = next(a for a in status["alerts"]
                    if a["code"] == "concurrency_over_memory")
        assert over["level"] == "bad"
