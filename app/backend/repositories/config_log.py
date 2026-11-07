"""选路配置的最小变更痕（0044）。

只回答一个问题：**"默认轨/登记/策略是被谁、在什么时候、从什么改成什么"**。
不是通用审计（那件事 v0.87 裁定不做，设计 §16 第 14 条）——业务表大部分
写路径仍然只有 `created_by` 一类弱引用，这里收窄到**选路**这一件事，
因为 `provider_config.is_default` 是 `resolve_provider()` 的唯一输入，
它变了而没人说，等于给"为什么这条作业走了云"埋一个无解的谜。

三条纪律：

1. **只追加**。改错了也是历史的一部分；最新结论由现在的表给出，
   这张表只回答"怎么走到现在的"。
2. **三个写点各记一行**，收在路由层（`routers/model_center.py` 与
   `routers/settings.py`）——服务层与仓储层不知道"人是谁"，
   路由层有 `user`，写点放那里才不会变成猜。
3. **不外键**。撤登记之后主体没了，这一行还要 standalone 地站着。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

SUBJECTS = ("provider_slot", "model_registration", "model_policy")


def record(subject: str, subject_key: str, action: str, summary: str,
           payload: dict | None = None, created_by: int | None = None) -> None:
    """记一行变更。**失败不炸主流程**：变更痕是锦上添花，
    不能让"改配置"因为日志写不进去而整批失败——那等于用日志劫持了业务。"""
    if subject not in SUBJECTS:
        raise ValueError(f"不认识的 subject：{subject}（认的只有 {SUBJECTS}）")
    if action not in ("update", "delete"):
        raise ValueError(f"不认识的 action：{action}")
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO config_change_log
                       (subject, subject_key, action, summary, payload_json, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (subject, subject_key, action, summary[:500],
                 json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                 created_by))
    except sqlite3.Error:
        # 表还没建（老库没跑 0044）或库暂时不可写：放行业务，痕迹这一笔认亏。
        # 不吞业务异常——那条该往上抛；吞的只有"日志自己写不进去"。
        pass


def recent(limit: int = 50) -> list[sqlite3.Row]:
    """最近改了什么，倒序一页。给"谁动过选路"一个直接的入口。"""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM config_change_log ORDER BY id DESC LIMIT ?""",
            (max(1, min(int(limit), 200)),)).fetchall()


def of_subject(subject: str, subject_key: str, limit: int = 50) -> list[sqlite3.Row]:
    """某一样东西的变更史。给详情面板/接口按 subject 取。"""
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM config_change_log
                WHERE subject = ? AND subject_key = ? ORDER BY id DESC LIMIT ?""",
            (subject, subject_key, max(1, min(int(limit), 200)))).fetchall()
