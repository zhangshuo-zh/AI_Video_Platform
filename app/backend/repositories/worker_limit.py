"""网页上设的并发上限：读写那一行，并把"人设的"与"进程自带的"分开说清（0047）。

三条纪律，都与这张表存在的理由绑在一起：

1. **NULL ≠ 1**。库里没有值时返回 None，语义是"没人设过，按 worker 自己
   （命令行或环境变量）走"；人明确设了 1 就存 1。混成一个值以后，
   `./run.sh worker --max-global 2` 与"网页上设了 2"，从库里读不出区别。
2. **只有一行**（`id = 1`，migration 里 CHECK 钉死）。多行就要回答"哪行生效"，
   而这张表就是为了消除这个猜测。
3. **不在这里判危险**。要不要拦、要不要勾"我知道"，归 `concurrency_plan`
   与路由层；这一层只管存取，改判定口径不该动到存储代码。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

SUBJECT = "worker_limit"


def get() -> dict | None:
    """读那一行。没设过返回 None（不是返回 global=1）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT global_max, per_json, updated_at, updated_by, note "
            "FROM worker_limit WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        per = json.loads(row["per_json"] or "{}")
    except ValueError:
        per = {}
    return {
        "global": row["global_max"],
        "per": per if isinstance(per, dict) else {},
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "note": row["note"] or "",
    }


def limits_or_none() -> dict | None:
    """给 worker 用的那一份：只有真的设过才返回。

    `global` 为 None 表示"人只设了每类上限、没设总数"——那时 `global` 这一项
    就不往回调给，让 `claim_next` 用它自己的默认，而不是被一个 None 顶掉。
    """
    row = get()
    if row is None or (row["global"] is None and not row["per"]):
        return None
    out: dict = {"per": dict(row["per"] or {})}
    if row["global"] is not None:
        out["global"] = int(row["global"])
    return out


def save(*, global_max: int | None, per: dict | None,
         updated_by: int | None = None, note: str = "") -> dict:
    """写那一行（幂等 upsert）。返回写完之后读回来的值。"""
    per_json = json.dumps(per or {}, ensure_ascii=False, sort_keys=True)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO worker_limit
                   (id, global_max, per_json, updated_at, updated_by, note)
               VALUES (1, ?, ?, datetime('now','localtime'), ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   global_max = excluded.global_max,
                   per_json  = excluded.per_json,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by,
                   note = excluded.note""",
            (global_max, per_json, updated_by, note[:200]))
    return get() or {}


def forget() -> None:
    """测试用：清掉那一行，回到"没人设过"。"""
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM worker_limit WHERE id = 1")
    except sqlite3.Error:
        pass
