#!/usr/bin/env python3
"""资产历史负载取样；仅创建临时迁移库，对比 0053 索引前后的结果、耗时与计划。"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.rows < 120 or args.repeats < 1:
        parser.error("rows 至少 120，repeats 至少 1")
    with tempfile.TemporaryDirectory(prefix="asset-history-load-") as temp:
        os.environ["FASTVIDEO_DB"] = str(Path(temp) / "test.db")
        from migrate import migrate
        migrate(verbose=False)
        from db import get_conn
        from models import ASSET_GEN_OPERATIONS
        from repositories.jobs import jobs
        from asset_generation_refs import parse_refs

        operations = tuple(ASSET_GEN_OPERATIONS)
        sample = [(json.dumps(["op:asset_image", f"asset:{1 if i % 10 == 0 else 2}"]),)
                  for i in range(args.rows)]
        insert = """INSERT INTO job(project_id,capability,kind,execution_mode,
                          provider_code,model_code,input_refs_json,status)
                    VALUES (1,'image','image','local','mock','mock-1',?,'succeeded')"""
        with get_conn() as conn:
            conn.execute("DROP INDEX idx_job_project_id")
            conn.execute("INSERT INTO app_user(username,display_name,password_hash) VALUES ('bench','bench','none')")
            conn.execute("INSERT INTO project(name,owner_id) VALUES ('bench',1)")
            conn.executemany(insert, sample)

        def measure(asset_id):
            samples = []
            for _ in range(args.repeats):
                before = time.perf_counter()
                rows = jobs.recent_asset_generations(1, operations, asset_id=asset_id, limit=12)
                samples.append((time.perf_counter() - before) * 1000)
            return {"median_ms": round(statistics.median(samples), 3),
                    "ids": [r["id"] for r in rows]}

        def plan():
            with get_conn() as conn:
                conn.create_function("asset_generation_matches", 1,
                    lambda raw: parse_refs({"input_refs_json": raw})["operation"] in operations,
                    deterministic=True)
                return [r["detail"] for r in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM job WHERE project_id=? "
                    "AND asset_generation_matches(input_refs_json) ORDER BY id DESC LIMIT ?", (1, 12))]

        def write_sample():
            values = []
            with get_conn() as conn:
                for _ in range(args.repeats):
                    before = time.perf_counter()
                    conn.execute("SAVEPOINT sample")
                    conn.executemany(insert, sample[:1000])
                    conn.execute("ROLLBACK TO sample")
                    conn.execute("RELEASE sample")
                    values.append((time.perf_counter() - before) * 1000)
            return round(statistics.median(values), 3)

        before_plan = plan()
        before = {str(a): measure(a) for a in (None, 1, 999)}
        before_write = write_sample()
        with get_conn() as conn:
            started = time.perf_counter()
            conn.execute("CREATE INDEX idx_job_project_id ON job(project_id,id DESC)")
            build_ms = round((time.perf_counter() - started) * 1000, 3)
        after = {str(a): measure(a) for a in (None, 1, 999)}
        after_write = write_sample()
        assert all(before[k]["ids"] == after[k]["ids"] for k in before)
        print(json.dumps({"rows": args.rows, "repeats": args.repeats,
                          "before": before, "after": after,
                          "before_plan": before_plan, "after_plan": plan(),
                          "index_build_ms": build_ms,
                          "insert_1000_rollback_ms": {"before": before_write, "after": after_write},
                          "selects_per_read": 1, "read_write_transactions": 0},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
