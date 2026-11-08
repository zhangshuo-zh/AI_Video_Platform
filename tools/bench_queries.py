#!/usr/bin/env python3
"""热查询的规模基准：同一份合成数据上，量回库次数、子进程次数、读行数与耗时。

为什么要有这个脚本：《发布基线》数的是"实现存在"（迁移号、表数、路由数、测试数），
不回答"这条读路径在几千行作业上扫了多少行"。本轮优化动的是 N+1 与索引，
"改好了"必须有可复核的数字，而不是看起来省了几次查询。

**只数机制，不吹性能**：次数（回库／子进程／读行）是这次优化的机制本身，
换台机器照样成立；毫秒只当参考——局域网笔记本上有调度噪声，
本脚本绝不把毫秒差写成"提速 X%"。

口径三条：
- 数据是合成的（项目→集→镜→成功视频作业），**不是真实模型产物**，跑完即删；
  它只回答 SQL 扫描规模，不回答"跑一集要几秒"。
- 候选索引由本脚本自己建、自己比：同一份数据、同一顺序、跑完就丢——
  现役库与仓库里的迁移一个都不碰。
- 被测的是仓储／服务层的函数本身，不含 HTTP 序列化。

用法：
    app/backend/.venv/bin/python tools/bench_queries.py
    app/backend/.venv/bin/python tools/bench_queries.py --episodes 60 --index job_episode
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "app" / "backend"

EPISODES = 25                   # × 60 镜 ＝ 1 500 镜（一季九集×60 之外再留一倍余量）
SHOTS_PER_EPISODE = 60          # 整集实测跑满的那一档（§17 量过 60 镜）
JOBS_PER_SHOT = 3               # ⇒ 4 500 条作业
ASSETS = 600                    # 任务书 §9 点名的量级：人物×形态×场景×道具凑到六百件

# 候选索引：建之前／建之后各量一遍，用 EXPLAIN 证明查询计划真的换了。
CANDIDATE_INDEXES = {
    "job_episode": ("idx_bench_job_episode",
                    "CREATE INDEX idx_bench_job_episode ON job(episode_id, status, shot_id)"),
    "audio_episode": ("idx_bench_audio_episode",
                      "CREATE INDEX idx_bench_audio_episode ON audio_clip(episode_id, status)"),
}


class FakeCursor:
    """`execute()` 被包装后必须还能 fetchall／fetchone／迭代。

    直接在真游标上 fetchall() 数完行数就返回，调用方拿到的是**已耗尽**的游标——
    那会让被测代码读到空列表，把基准测成"什么都没查"的假象。
    """

    def __init__(self, rows, description):
        self._rows = list(rows)
        self.description = description
        self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class Counter:
    """给 `sqlite3.connect(...)`、`subprocess.run()` 与**文件系统探测**各挂一层计数。

    `fs` 这一维是 2026-11-08 补的：原来只数回库／子进程，而首页工作台的
    「产物待签」会对着每条产物做 `is_file()` + `stat()`（`jobfiles.artifacts()`），
    文档待签还要整篇读正文算 sha256。那些开销在旧基准里**完全隐形**——
    表格上写着"回库 3 次"很好看，实际可能在打几千次 stat。
    数不到的东西就不会被优化，所以这一维必须补上。

    为什么包在 `Connection` 子类上而不是往实例上贴属性：`sqlite3.Connection.execute`
    是 C 层只读属性，`conn.execute = wrapped` 会直接 AttributeError（本机实测）。
    `connect(..., factory=...)` 是唯一能换掉它的入口，而 `db.py` 用的是默认工厂，
    所以这里只在基准进程里换，被测代码一行不动。
    """

    def __init__(self) -> None:
        self.queries = 0
        self.probes = 0
        self.rows = 0
        self.fs = 0            # is_file()/stat()/read_text() 这类文件系统探测次数
        self._orig_connect = sqlite3.connect
        self._orig_run = subprocess.run
        self._orig_stat = Path.stat
        self._orig_is_file = Path.is_file
        self._orig_read_text = Path.read_text
        outer = self

        class CountingConnection(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                outer.queries += 1
                cur = super().execute(sql, *a, **kw)
                if cur.description is None:            # 写操作：没有行可数
                    return cur
                rows = cur.fetchall()
                outer.rows += len(rows)
                return FakeCursor(rows, cur.description)

        self._factory = CountingConnection

    def install(self) -> None:
        def connect(*a, **kw):
            kw.setdefault("factory", self._factory)
            return self._orig_connect(*a, **kw)

        def run(cmd, *a, **kw):
            self.probes += 1
            return self._orig_run(cmd, *a, **kw)

        # 闭包里用 self（Counter 实例）而不是 outer：
        # `outer` 是在 __init__ 里定义的局部别名，install() 看不到它。
        def stat(self_, *a, **kw):
            self.fs += 1
            return self._orig_stat(self_, *a, **kw)

        def is_file(self_, *a, **kw):
            self.fs += 1
            return self._orig_is_file(self_, *a, **kw)

        def read_text(self_, *a, **kw):
            self.fs += 1
            return self._orig_read_text(self_, *a, **kw)

        sqlite3.connect = connect                      # type: ignore[assignment]
        subprocess.run = run                           # type: ignore[assignment]
        Path.stat = stat                               # type: ignore[assignment]
        Path.is_file = is_file                         # type: ignore[assignment]
        Path.read_text = read_text                     # type: ignore[assignment]

    def uninstall(self) -> None:
        sqlite3.connect = self._orig_connect           # type: ignore[assignment]
        subprocess.run = self._orig_run                # type: ignore[assignment]
        Path.stat = self._orig_stat                    # type: ignore[assignment]
        Path.is_file = self._orig_is_file              # type: ignore[assignment]
        Path.read_text = self._orig_read_text          # type: ignore[assignment]

    def reset(self) -> None:
        self.queries = self.probes = self.rows = self.fs = 0


def build_world(episodes: int, shots_per_ep: int, jobs_per_shot: int,
                assets: int = ASSETS) -> dict:
    """在临时库里搭项目→集→镜→成功视频作业→成片版本→产线运行，返回规模与抽样 id。

    成片与产线也要搭：这两条列表路径如果库里没有行，测出来的"回库 3 次"是真的快，
    也是假的准——N+1 只在有 N 条的时候才露头。
    """
    conn = sqlite3.connect(os.environ["FASTVIDEO_DB"])
    conn.execute("PRAGMA foreign_keys=OFF")
    uid = conn.execute("SELECT id FROM app_user ORDER BY id LIMIT 1").fetchone()[0]
    rng = random.Random(20261103)          # 固定种子：两遍跑面对同一份数据
    job_id = 1000
    comp_id = 400000
    run_id = 500000
    node_id = 600000
    tmpl = conn.execute("SELECT id FROM pipeline_template ORDER BY id LIMIT 1").fetchone()
    if tmpl is None:
        raise SystemExit("临时库里没有内置产线模板——基准需要它来搭运行")
    template_id = tmpl[0]
    sample = {"project_ids": [], "episode_ids": [], "shot_ids": [],
              "referenced_asset_ids": []}
    timeline = json.dumps({"schema": 1, "shots": [], "audio": [], "subtitles": [],
                           "total_duration_ms": 12000}, ensure_ascii=False)
    # 资产要按项目铺：预检与"源已变"读的是**本镜引用的那几件**，
    # 库里只有三件全局资产时"逐资产回库"和"一次批量"都只打一趟，量不出差别。
    # 每项目 24 件、kind 轮着来，三分之一没有参考图、三分之一没有一致性提示词
    # ——那两档正是 precheck 里会走 WARN 分支的路径，空着就没被测到。
    project_assets: dict[int, list[int]] = {}
    aid = 700000
    for a in range(assets):
        owner = 5000 + (a % episodes)          # 与下面建项目的 id 规则对齐
        conn.execute(
            """INSERT INTO asset (id, project_id, kind, name, summary, consistency_prompt,
                                  ref_images, created_at, updated_at)
               VALUES (?, ?, ?, ?, '基准用', ?, ?,
                       datetime('now','localtime'), datetime('now','localtime'))""",
            (aid, owner, ("character", "scene", "prop")[a % 3], f"基准资产_{a}",
             "" if a % 3 == 1 else "黑发、灰袍",
             "[]" if a % 3 == 2 else json.dumps([f"assets/{aid}.png"])))
        project_assets.setdefault(owner, []).append(aid)
        aid += 1
    for e in range(episodes):
        pid, eid = 5000 + e, 9000 + e
        conn.execute(
            """INSERT INTO project (id, name, status, owner_id, created_at, updated_at)
               VALUES (?, ?, 'active', ?, datetime('now','localtime'),
                       datetime('now','localtime'))""", (pid, f"基准项目_{e}", uid))
        conn.execute(
            """INSERT INTO episode (id, project_id, seq, title, created_at)
               VALUES (?, ?, 1, ?, datetime('now','localtime'))""", (eid, pid, f"第 {e + 1} 集"))
        sample["project_ids"].append(pid)
        sample["episode_ids"].append(eid)
        ep_job_ids = []
        pool = project_assets[pid]
        # 抽样一个"确实被本项目的镜头引用着"的资产：asset_impact 只有在这种资产上
        # 才会走到反查那一支，拿没被引用的资产量等于什么都没量。
        sample.setdefault("referenced_asset_ids", []).append(pool[0])
        for s in range(shots_per_ep):
            sid = eid * 100 + s
            conn.execute(
                """INSERT INTO shot (id, episode_id, seq, summary, prompt, duration,
                                     asset_ids, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 2.0, ?, 1, datetime('now','localtime'),
                           datetime('now','localtime'))""",
                (sid, eid, s + 1, f"镜头 {s + 1} 内容", f"镜头 {s + 1} 提示词",
                 json.dumps([pool[s % len(pool)], pool[(s + 7) % len(pool)]])))
            sample["shot_ids"].append(sid)
            for k in range(jobs_per_shot):
                job_id += 1
                ep_job_ids.append(job_id)
                conn.execute(
                    """INSERT INTO job (id, project_id, episode_id, shot_id, capability,
                               kind, execution_mode, provider_code, model_code,
                               adapter_version, prompt_snapshot, params_json,
                               input_refs_json, status, priority, idempotency_key,
                               attempt, queued_at, started_at, finished_at, elapsed_ms,
                               billable_status, cost_micros, currency, created_by,
                               created_at, updated_at, resources_json, source_version,
                               result_paths_json)
                       VALUES (?, ?, ?, ?, 'video', 'video_local', 'local', 'mock',
                               'mock-1', 'v1', '提示词', '{}', '[]', 'succeeded',
                               ?, ?, 1, datetime('now','localtime'),
                               datetime('now','localtime'), datetime('now','localtime'),
                               1200, 'not_billable', 0, 'CNY', ?,
                               datetime('now','localtime'), datetime('now','localtime'),
                               '{}', ?, ?)""",
                    (job_id, pid, eid, sid, rng.randint(1, 5), f"bench-{job_id}",
                     uid, f"sv-{job_id}",
                     # 产物路径要给真值（2026-11-08）：原来是 `'[]'`，
                     # 于是「首页工作台／作业详情」里 `jobfiles.artifacts()`
                     # 对每条产物做的 is_file()+stat() 一次都不执行——
                     # 文件探测那一列恒为 0，那部分开销在基准里是隐形的。
                     # 现在每条成功作业挂一条产物路径，`stat` 会真的打出去。
                     json.dumps([f"{job_id}/outputs/out.mp4"], ensure_ascii=False)))
        # 每集两个成片版本（一条挂在真实 Job 上），让成片列表的 N+1 有东西可露
        for version in (1, 2):
            comp_id += 1
            conn.execute(
                """INSERT INTO composition (id, project_id, episode_id, pipeline_run_id,
                                            version, timeline_json, params_json, job_id,
                                            output_path, sha256, bytes, duration_ms,
                                            width, height, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, NULL, ?, ?, '{}', ?, 'x', 'deadbeef', 1024, 12000,
                           1280, 720, ?, datetime('now','localtime'),
                           datetime('now','localtime'))""",
                (comp_id, pid, eid, version, timeline,
                 ep_job_ids[version - 1] if len(ep_job_ids) >= version else None, uid))
        # 每集一条已收尾的产线运行＋每镜一个节点，让运行列表的 N+1 有东西可露
        run_id += 1
        conn.execute(
            """INSERT INTO pipeline_run (id, project_id, episode_id, template_id,
                     template_version, input_snapshot_json, idempotency_key, created_by,
                     paused, cancelled, started_at, finished_at, created_at)
                   VALUES (?, ?, ?, ?, 1, '{}', ?, ?, 0, 0,
                           datetime('now','localtime'), datetime('now','localtime'),
                           datetime('now','localtime'))""",
            (run_id, pid, eid, template_id, f"bench-run-{run_id}", uid))
        for s in range(shots_per_ep):
            node_id += 1
            conn.execute(
                """INSERT INTO pipeline_node_run (id, run_id, node_key, node_type, label,
                         depends_on_json, status, job_id, retry_count, created_at)
                   VALUES (?, ?, ?, 'video_generate', '镜头视频', '[]', 'completed',
                           ?, ?, datetime('now','localtime'))""",
                (node_id, run_id, f"video_generate#s{s}",
                 ep_job_ids[s] if s < len(ep_job_ids) else None,
                 1 if s == 0 else 0))
        # ---------------------------------------------------------------
        # 排队作业：给 `claim_next()` 那条基准用（2026-11-08 补）
        #
        # 上面造的全是 `succeeded`，那是"读历史"的数据；而 `claim_next` 读的是
        # `WHERE status='queued'` 这一档，不造就没有行可领——基准会测成
        # "回库 2 次、读 0 行"的假快。这里每集造 40 条 queued，跨四种能力与
        # 三种资源类（video/image/audio/compose 各占一条槽），
        # 好让"按资源类挑一条"这件事真的有候选可挑。
        # ---------------------------------------------------------------
        for s2 in range(40):
            job_id += 1
            # `kind` 有 CHECK 约束（0015），只能用那六个合法值；
            # 「哪批是基准造的排队作业」靠 `idempotency_key` 前缀 `bench-q-` 认，
            # 重置时也按它 UPDATE（见 main 里的 `_reset_queue`）。
            cap, kind, mode, prov = (
                ("video", "video_local", "local", "mock") if s2 % 4 == 0 else
                ("image", "image", "local", "mock") if s2 % 4 == 1 else
                ("tts", "tts", "local", "mock") if s2 % 4 == 2 else
                ("video", "video_local", "local", "ffmpeg"))
            conn.execute(
                """INSERT INTO job (id, project_id, episode_id, shot_id, capability,
                           kind, execution_mode, provider_code, model_code,
                           adapter_version, prompt_snapshot, params_json,
                           input_refs_json, status, priority, idempotency_key,
                           attempt, queued_at, billable_status, created_by,
                           created_at, updated_at, resources_json, source_version,
                           result_paths_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'mock-1', 'v1',
                           '提示词', '{}', '[]', 'queued', ?, ?, 1,
                           datetime('now','localtime'), 'not_billable', ?,
                           datetime('now','localtime'), datetime('now','localtime'),
                           '{}', ?, '[]')""",
                (job_id, pid, eid,
                 sample["shot_ids"][s2 % max(1, len(sample["shot_ids"]))]
                 if sample["shot_ids"] else None,
                 cap, kind, mode, prov, rng.randint(1, 5), f"bench-q-{job_id}", uid,
                 f"sv-q-{job_id}"))
        sample.setdefault("queued_job_ids", []).append(job_id)

    conn.commit()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("project", "episode", "shot", "job", "composition",
                        "pipeline_run", "pipeline_node_run", "asset")}
    counts["queued_job"] = conn.execute(
        "SELECT COUNT(*) FROM job WHERE status = 'queued'").fetchone()[0]
    conn.close()
    return {"sample": sample, "counts": counts}


def measure(label: str, fn, counter: Counter) -> dict:
    """跑 5 次取中位毫秒，次数取最后一次（每次调用做的活一样）。"""
    timings, last = [], None
    for _ in range(5):
        counter.reset()
        t0 = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - t0) * 1000)
        last = (counter.queries, counter.probes, counter.rows, counter.fs)
    timings.sort()
    return {"label": label, "queries": last[0], "probes": last[1], "rows": last[2],
            "fs": last[3], "ms": round(timings[len(timings) // 2], 2)}


def table(rows: list[dict]) -> None:
    print("| 读路径 | 回库次数 | 子进程次数 | 读行数 | 文件探测 | 中位毫秒 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['label']} | {r['queries']} | {r['probes']} | {r['rows']} "
              f"| {r['fs']} | {r['ms']} |")


def explain(conn: sqlite3.Connection, title: str, sql: str, params) -> None:
    steps = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    print(f"- {title}：{'；'.join(steps)}")


def _probe_migrate_race(*, rounds: int = 5) -> dict:
    """两个进程同时对**同一个全新空库**跑 `migrate()`，数有多少轮失败。

    为什么要起子进程而不是用线程：`migrate()` 每次自己 `sqlite3.connect`，
    同进程内两条连接也能并发，但那不模拟真实场景——真实场景就是
    「API 进程和 worker 进程同时启动」（`main.py` 与 `worker.py` 各调一次）。
    """
    ok = fail = 0
    details: list[str] = []
    code = (
        "import sys, os;"
        "sys.path.insert(0, sys.argv[1]);"
        "import migrate;"
        "migrate.migrate(verbose=False)"
    )
    for i in range(rounds):
        d = Path(tempfile.mkdtemp(prefix="fv-migrace-"))
        env = {**os.environ, "FASTVIDEO_DB": str(d / "race.db")}
        procs = [
            subprocess.Popen([sys.executable, "-c", code, str(BACKEND)],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(2)
        ]
        outs = [p.communicate() for p in procs]
        bad = [(p.returncode, out[1].decode("utf-8", "replace")[-200:])
               for p, out in zip(procs, outs) if p.returncode != 0]
        if bad:
            fail += 1
            details.append(f"第 {i + 1} 轮：rc={bad[0][0]} {bad[0][1].strip()[:120]}")
        else:
            ok += 1
        shutil.rmtree(d, ignore_errors=True)
    return {"rounds": rounds, "ok": ok, "fail": fail, "details": details}


def main() -> int:
    ap = argparse.ArgumentParser(description="热查询规模基准（临时库，不碰现役数据）")
    ap.add_argument("--episodes", type=int, default=EPISODES)
    ap.add_argument("--shots-per-episode", type=int, default=SHOTS_PER_EPISODE)
    ap.add_argument("--jobs-per-shot", type=int, default=JOBS_PER_SHOT)
    ap.add_argument("--assets", type=int, default=ASSETS,
                    help="资产件数（按项目平铺；600 是任务书 §9 点名的量级）")
    ap.add_argument("--index", action="append", default=[], choices=sorted(CANDIDATE_INDEXES),
                    help="只建这些候选索引再量一遍（可重复）")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="fv-bench-"))
    os.environ["FASTVIDEO_DB"] = str(tmp / "bench.db")
    os.environ["FASTVIDEO_JOBS"] = str(tmp / "jobs")
    os.environ["FASTVIDEO_FILES"] = str(tmp / "files")
    os.environ["FASTVIDEO_WORKSPACE"] = str(tmp / "workspace")
    (tmp / "jobs").mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(BACKEND))
    import migrate
    from bootstrap import ensure_admin
    migrate.migrate(verbose=False)
    ensure_admin(verbose=False)     # 迁移只建表；超级用户是 main.py 启动时补的，基准也要有
    import pipeline as _pl
    _pl.ensure_builtin_templates()  # 内置模板同样是启动时补的，搭运行记录要用它
    world = build_world(args.episodes, args.shots_per_episode, args.jobs_per_shot,
                        args.assets)

    import pipeline
    import precheck as _pre
    import production as _prod
    import services
    from composition import service as composition_service
    from db import get_conn
    from repositories.compositions import compositions
    from repositories.content import assets as asset_repo
    from repositories.jobs import jobs as job_repo
    from repositories.users import users

    # "这一镜成片会用哪条视频作业"这个选择器在 v0.97 之前住在成片仓储里、之后搬到
    # Job 仓储（Job 表只有一个东家）。基准要能在改动前后两个树上跑同一份脚本，
    # 所以这里按名字找，找不到就用另一个出处——量的都是同一条 SQL 的形状。
    video_selector = (getattr(compositions, "latest_video_jobs", None)
                      or job_repo.latest_succeeded_videos_by_shot)

    c = world["counts"]
    ep, pid = world["sample"]["episode_ids"][0], world["sample"]["project_ids"][0]
    admin = users.get_by_id(1)
    print(f"规模：项目 {c['project']}／集 {c['episode']}／镜 {c['shot']}／"
          f"资产 {c['asset']}／成功视频作业 {c['job']}（临时库 {tmp.name}，跑完即删）\n")

    with get_conn() as conn0:
        run_by_episode = {int(r["episode_id"]): int(r["id"]) for r in conn0.execute(
            "SELECT episode_id, id FROM pipeline_run")}

    def _run_for(episode_id: int) -> int:
        return run_by_episode[episode_id]

    # ---------------------------------------------------------------
    # 反复测量的辅助：`claim_next` 会**改数据**（把 queued 领成 starting），
    # 直接连测 5 次会让第 2 次起没行可领，测出来的"回库 2 次"是假的。
    # 所以每次测量前把它复位回 queued —— 这样 5 次面对同一份数据，
    # 与其它只读 case 的可比性才成立。
    # ---------------------------------------------------------------
    def _reset_queue() -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'queued', worker_id = NULL,
                          lease_until = NULL, started_at = NULL
                    WHERE idempotency_key LIKE 'bench-q-%'""")

    def _claim_once():
        _reset_queue()
        return job_repo.claim_next("bench-worker", 60)

    # 收尾工作台那两格之前先清一次，别让上一轮的 starting 留在表里
    _reset_queue()

    cases = [
        ("分镜列表 shots_view(一集)", lambda: services.shots_view(ep)),
        ("成片·最新成功视频作业(一集)", lambda: video_selector(ep)),
        ("生产前检查 preflight(一集)", lambda: pipeline.preflight(pid, ep, None)),
        # 这一条现在也会统计「文件探测」一列：`_pending_artifacts_for` 对每条产物
        # 做 is_file()+stat()，`_pending_documents_for` 整篇读正文算 sha256。
        # 旧基准只数 SQL，那部分开销是隐形的（2026-11-08 补了 fs 维度）。
        ("首页工作台 workbench", lambda: services.workbench(admin)),
        ("成片列表 list_for_project", lambda: composition_service.list_for_project(pid)),
        ("产线运行列表 runs_for_project", lambda: pipeline.runs_for_project(pid)),
        ("产线运行详情 run_view(一集)", lambda: pipeline.run_view(_run_for(ep))),
        # ↓ 生产主线改造（0037／0038）新长出来的那几条读路径：
        #   这一批是任务书 §9 点名"600 资产／1 500 镜头量级下没测过"的正主——
        #   工作台、预检、统计、资产影响都是**每次打开页面就跑**的，
        #   它们里藏一次 N+1，人每天点几百次就是在替服务器数行数。
        ("项目工作台 project_overview(60 镜那一集)",
         lambda: _prod.project_overview(pid, current_episode_id=ep)),
        ("九阶段与下一步 episode_board(一集)", lambda: _prod.episode_board(pid, ep)),
        ("生产统计 project_stats(项目级)", lambda: _prod.project_stats(pid)),
        ("镜头工作台 shot_workbench(一镜)",
         lambda: _prod.shot_workbench(world["sample"]["shot_ids"][0])),
        ("生成前预检 shot_precheck(一镜)",
         lambda: _pre.shot_precheck(world["sample"]["shot_ids"][0])),
        ("整集预检 episode_precheck(一集)", lambda: _pre.episode_precheck(pid, ep)),
        ("合成前预检 composition_precheck(一集)",
         lambda: _pre.composition_precheck(pid, ep)),
        ("资产列表 list_visible(项目 24 件＋全局池)",
         lambda: asset_repo.list_visible(pid)),
        ("资产影响 asset_impact(被镜头引用的那件)",
         lambda: _prod.asset_impact(world["sample"]["referenced_asset_ids"][0])),
        # ↓↓ 2026-11-08 补的四条：原先基准**不测**这几条路径，
        #    于是一直没有依据判断它们要不要优化。
        #
        # ① 队列领取：worker 每 ~1 秒调一次，是唯一"空转也要花成本"的读路径。
        #    它当前把全部 queued 行 fetchall 进 Python 再挑第一条。
        ("队列领取 claim_next(worker 每轮)", _claim_once),
        # ② 成本账：管理员视图不筛项目时 where="1"，等于全库。
        #    当前把全部本地作业明细回传进内存。
        ("成本账 cost_summary(全库)", lambda: job_repo.cost_summary(None)),
        ("成本账 cost_summary(单项目)", lambda: job_repo.cost_summary([pid])),
        # ③ 按集读作业的分页查询（生成中心列表用）——它走 idx_job_episode_status，
        #    是 0032 那条索引的直接受益者，留一条基线便于以后对照。
        ("按集读作业 list(一集, limit 200)",
         lambda: job_repo.list([pid], episode_id=ep, limit=200)),
    ]

    counter = Counter()
    counter.install()
    try:
        before = [measure(label, fn, counter) for label, fn in cases]
    finally:
        counter.uninstall()
    print("## 优化前\n")
    table(before)

    wanted = args.index or sorted(CANDIDATE_INDEXES)
    conn = sqlite3.connect(os.environ["FASTVIDEO_DB"])
    print("\n## 建索引前·查询计划")
    explain(conn, "shots_view 的作业子查询",
            """SELECT j.* FROM job j JOIN (SELECT shot_id, MAX(id) AS mid FROM job
               WHERE episode_id = ? AND shot_id IS NOT NULL AND status = 'succeeded'
               GROUP BY shot_id) t ON t.mid = j.id""", (ep,))
    explain(conn, "生成中心按集筛作业",
            "SELECT * FROM job WHERE episode_id = ? ORDER BY id DESC LIMIT 200", (ep,))
    conn.close()

    conn = sqlite3.connect(os.environ["FASTVIDEO_DB"])
    for key in wanted:
        _, ddl = CANDIDATE_INDEXES[key]
        conn.execute(ddl)
    conn.commit()
    print("\n## 建索引后·查询计划")
    explain(conn, "shots_view 的作业子查询",
            """SELECT j.* FROM job j JOIN (SELECT shot_id, MAX(id) AS mid FROM job
               WHERE episode_id = ? AND shot_id IS NOT NULL AND status = 'succeeded'
               GROUP BY shot_id) t ON t.mid = j.id""", (ep,))
    explain(conn, "生成中心按集筛作业",
            "SELECT * FROM job WHERE episode_id = ? ORDER BY id DESC LIMIT 200", (ep,))

    counter.install()
    try:
        after = [measure(label, fn, counter) for label, fn in cases]
    finally:
        counter.uninstall()
    conn.close()

    print("\n## 候选索引就位后（同一份数据）\n")
    table(after)

    print("\n## 逐条差值（次数与读行数才是结论，毫秒只当参考）\n")
    for b, a in zip(before, after):
        print(f"- {b['label']}：回库 {b['queries']}→{a['queries']}，"
              f"读行 {b['rows']}→{a['rows']}，子进程 {b['probes']}→{a['probes']}，"
              f"文件探测 {b['fs']}→{a['fs']}，中位 {b['ms']}ms→{a['ms']}ms")

    # ---------------------------------------------------------------
    # 迁移并发探测（2026-11-08 补）
    #
    # 这一条量的是**机制**而不是扫描量：`migrate()` 读 `schema_version`
    # 在任何事务之外，两个进程同时启动时可能都读到同一份 applied 集合，
    # 然后同时应用同一个版本——后一个会撞主键而回滚抛错（幂等性只对串行成立）。
    #
    # 做法：全新空库 + 两个进程同时 `migrate()`，各跑若干轮，报"失败/成功"。
    # 这不是性能数字，是**可靠性事实**：几轮里出现非零退出，就说明窗口真实存在。
    # ---------------------------------------------------------------
    races = _probe_migrate_race(rounds=5)
    print("\n## 迁移并发探测（全新空库，两进程同时 migrate）\n")
    print(f"- 轮次：{races['rounds']}；两进程同时启动")
    print(f"- 成功：{races['ok']}；失败：{races['fail']}")
    if races["fail"]:
        print("- **检出竞态**：下面这些轮次里至少一个进程非零退出。")
        print("  两条独立的窗口，错误信息不同：")
        print("    · `_connect()` 的 `PRAGMA journal_mode=WAL`（并发首次建库争锁；")
        print("      PRAGMA 不受 busy_timeout 保护，timeout=5.0 在这里不生效）")
        print("    · 迁移末尾 `INSERT INTO schema_version`（两个进程读到同一份 applied，")
        print("      后一个撞主键）")
        for detail in races["details"][:3]:
            print(f"    {detail}")
    else:
        print("- 未检出竞态（本机时序下没撞上；不代表窗口不存在，只代表这几轮没踩到）")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
