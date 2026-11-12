"""库结构层面的守卫：外键的**子表侧**也要有索引。

为什么单开一份文件：这一类断言查的是 `PRAGMA foreign_key_list`／`index_list`，
跟 test_infra.py 那种"扫源码文本"的守卫是两种取证方式，混在一起以后
谁都想不起来该往哪边加。

每一条都要能证伪：断言写具体集合，不写"应该有索引"这种查不动的话。
"""
from db import get_conn

# 点名豁免：一张表只有一行左右时，级联查找的"整表扫"成本就是零，
# 加索引只是多养一棵 B 树。加进这张表要写清为什么，不许默默少一条。
_JOB_CHILD_EXEMPT = {
    ("worker_node", "current_job_id"),   # 一台 worker 一行，0036 里写明了理由
}


def test_挂作业的子表外键列都有索引():
    """删一行 job 时，SQLite 得先在每张子表里找出该置 NULL／该级联掉的子行。

    子列没索引＝**整表扫**。0036 之前 `asset_variant.source_job_id`、`audio_clip.job_id`、
    `model_validation.job_id` 三条都没索引，实测（2400 行作业＋5600 行子行，
    删 1—2000 号作业再回滚，**两次独立进程、只差这三条索引**）：
    无索引 **522.7／528.6 ms**，有索引 **71.0／121.7 ms**。要紧的不是那零点几秒，
    是删项目／删集都跑在写事务里，半秒级持锁对局域网 2—5 人就是别人提交时那句
    "database is locked"。（同一进程里"先删再建索引再删"量不出这个差：
    页缓存与语句缓存会把差抹成负数，0036 的注释里记了这一笔。）

    这条守卫不数"索引有几条"，直接把外键关系全数出来逐条问：这张表上有没有一条
    索引以那个外键列**打头**。下次再开一张挂作业的子表而忘了索引，当场红。
    """
    with get_conn() as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        assert tables, "库里一张表都没有，这条测试在测空气"

        offenders = []
        for t in tables:
            fks = [fk["from"] for fk in list(conn.execute(f"PRAGMA foreign_key_list({t})"))
                   if fk["table"] == "job" and fk["from"]]
            if not fks:
                continue
            leading = set()
            for ix in list(conn.execute(f"PRAGMA index_list({t})")):
                if ix["name"] is None:
                    continue
                first = conn.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno LIMIT 1",
                    (ix["name"],)).fetchone()
                if first:
                    leading.add(first["name"])
            for col in fks:
                # leading 装的是**列名**，比的时候别把表名一起塞进去——
                # 写成 `(t, col) not in leading` 会让八条"其实有索引"的子列全红，
                # 而且红得很像"这库真缺索引"，很容易照着它去补一条根本不缺的索引
                if col not in leading and (t, col) not in _JOB_CHILD_EXEMPT:
                    offenders.append(f"{t}.{col}")

        assert not offenders, (
            f"这些指向 job(id) 的子列没有可用的打头索引，删一行作业就要整表扫一遍："
            f"{sorted(offenders)}——补索引（参考 0036）或写进豁免名单并说明理由")


def test_豁免名单不是空架子():
    """豁免的每一条必须真的是"没索引也不该有"，否则这行名单会悄悄变成后门。

    做了两件事：① 豁免的列必须确实存在且确实**没有**打头索引（有人补了索引就该把它
    从名单里删掉，不然名单只会越写越长）；② 必须写明理由——这里用"注释里出现表名"
    当最低门槛，真正的判据在 0036 的迁移注释里。
    """
    with get_conn() as conn:
        for t, col in _JOB_CHILD_EXEMPT:
            fks = [fk["from"] for fk in list(conn.execute(f"PRAGMA foreign_key_list({t})"))
                   if fk["table"] == "job"]
            assert col in fks, f"{t}.{col} 已经不是 job 的外键子列了，从豁免名单删掉它"
            leading = set()
            for ix in list(conn.execute(f"PRAGMA index_list({t})")):
                first = conn.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno LIMIT 1",
                    (ix["name"],)).fetchone()
                if first:
                    leading.add(first["name"])
            assert col not in leading, (
                f"{t}.{col} 已经有打头索引了，豁免名单该删这一行——留着就没人再看它")


def test_项目作业历史按ID倒序不需要临时排序():
    with get_conn() as conn:
        plan = [r["detail"] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM job WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (1, 12))]
    assert any("idx_job_project_id" in row for row in plan), plan
    assert not any("TEMP B-TREE" in row for row in plan), plan


def test_0052旧库升级历史索引后数据不变且可重复迁移(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = '''
import migrate, sqlite3
from config import DB_PATH
original=migrate._discover
migrate._discover=lambda:[(v,p) for v,p in original() if v<=52]
migrate.migrate(verbose=False)
with sqlite3.connect(DB_PATH) as conn:
    conn.execute("INSERT INTO app_user(username,display_name,password_hash) VALUES ('old','old','none')")
    conn.execute("INSERT INTO project(name,owner_id) VALUES ('old',1)")
    conn.execute("INSERT INTO job(project_id,capability,kind,execution_mode,provider_code,model_code,input_refs_json,status) VALUES(1,'image','image','local','mock','mock-1','[\\\"op:asset_image\\\",\\\"asset:1\\\"]','queued')")
    before=conn.execute('SELECT * FROM job').fetchall()
migrate._discover=original
assert migrate.migrate(verbose=False)==1
assert migrate.migrate(verbose=False)==0
with sqlite3.connect(DB_PATH) as conn:
    assert conn.execute('SELECT * FROM job').fetchall()==before
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_job_project_id'").fetchone()
backups=[p for p in DB_PATH.parent.glob(DB_PATH.name+'.bak-*') if not p.name.endswith(('-wal','-shm'))]
verified=0
for backup in backups:
    with sqlite3.connect(backup) as conn:
        if conn.execute('SELECT max(version) FROM schema_version').fetchone()[0]!=52:
            continue
        assert conn.execute('SELECT * FROM job').fetchall()==before
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_job_project_id'").fetchone()
        verified+=1
assert verified>=1
'''
    result = subprocess.run([sys.executable, "-c", code],
                            cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, "FASTVIDEO_DB": str(tmp_path / "upgrade.db")},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
