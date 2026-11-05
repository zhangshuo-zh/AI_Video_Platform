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
    """给 `sqlite3.connect(...)` 与 `subprocess.run()` 各挂一层计数。

    为什么包在 `Connection` 子类上而不是往实例上贴属性：`sqlite3.Connection.execute`
    是 C 层只读属性，`conn.execute = wrapped` 会直接 AttributeError（本机实测）。
    `connect(..., factory=...)` 是唯一能换掉它的入口，而 `db.py` 用的是默认工厂，
    所以这里只在基准进程里换，被测代码一行不动。
    """

    def __init__(self) -> None:
        self.queries = 0
        self.probes = 0
        self.rows = 0
        self._orig_connect = sqlite3.connect
        self._orig_run = subprocess.run
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

        sqlite3.connect = connect                      # type: ignore[assignment]
        subprocess.run = run                           # type: ignore[assignment]

    def uninstall(self) -> None:
        sqlite3.connect = self._orig_connect           # type: ignore[assignment]
        subprocess.run = self._orig_run                # type: ignore[assignment]

    def reset(self) -> None:
        self.queries = self.probes = self.rows = 0


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
    per_project = max(1, assets // episodes)
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
                               '{}', ?, '[]')""",
                    (job_id, pid, eid, sid, rng.randint(1, 5), f"bench-{job_id}",
                     uid, f"sv-{job_id}"))
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
    conn.commit()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("project", "episode", "shot", "job", "composition",
                        "pipeline_run", "pipeline_node_run", "asset")}
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
        last = (counter.queries, counter.probes, counter.rows)
    timings.sort()
    return {"label": label, "queries": last[0], "probes": last[1], "rows": last[2],
            "ms": round(timings[len(timings) // 2], 2)}


def table(rows: list[dict]) -> None:
    print("| 读路径 | 回库次数 | 子进程次数 | 读行数 | 中位毫秒 |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['label']} | {r['queries']} | {r['probes']} | {r['rows']} | {r['ms']} |")


def explain(conn: sqlite3.Connection, title: str, sql: str, params) -> None:
    steps = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    print(f"- {title}：{'；'.join(steps)}")


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

    import services
    import pipeline
    import precheck as _pre
    import production as _prod
    from repositories.content import assets as asset_repo
    from composition import service as composition_service
    from repositories.compositions import compositions
    from repositories.jobs import jobs as job_repo
    from repositories.users import users
    from db import get_conn

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

    cases = [
        ("分镜列表 shots_view(一集)", lambda: services.shots_view(ep)),
        ("成片·最新成功视频作业(一集)", lambda: video_selector(ep)),
        ("生产前检查 preflight(一集)", lambda: pipeline.preflight(pid, ep, None)),
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
              f"中位 {b['ms']}ms→{a['ms']}ms")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
