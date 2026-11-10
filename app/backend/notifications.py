"""站内通知：谁该被告知、通知怎么落到人头上、已读怎么算（0049）。

这个模块只回答三件事，每件都有它必须这样的理由：

**1. 收件人是"指向"而不是"展开"**
`audience` 存成 `project:<id>`，查询时才解析成"当前的项目所有者 + 协作者"。
不展开成多行的理由：成员会变。展开的话，退出项目的人还留着他那份副本
（数据泄漏），新加入的人看不到历史（倒还算合理）。指向项目则两头都自然。

**2. 同一件事不重复打扰**
worker 掉线每 15 秒检查一次，若每轮发一条，几分钟就是几十条。
所以 `emit(..., dedupe_key=...)` 会先看"有没有一条同类未结的"，
有就不再发。**但"结清"要显式做**（`resolve`）——否则修好之后再掉一次
就发不出来了，那比重复更糟（真出事时没提醒）。

**3. 已读按人记**
一条通知可能被多个项目成员看到，各自标记已读互不影响。
所以 `notification_read` 是一张 (notification_id, user_id) 的表，
不是 notification 上的一个 `read_at` 列。
"""
from __future__ import annotations

import json

from db import get_conn

# 能出现在界面上的 kind。加新的要同时改 `js/notifications.js` 的图标映射，
# 否则会显示成一个没有图标的条目——不报错，但看着像坏了。
KINDS = ("worker_offline", "worker_daemon", "job_failed", "concurrency_risk")

LEVELS = ("info", "warn", "bad")

# 每种 kind 点进去该去哪一页。存的是**页面 id**（与 `modules.js` 一致），
# 不是完整 URL——端口和域名会变，路径不会。
DEFAULT_LINKS = {
    "worker_offline": ("models", {"sub": "运行监控"}),
    "worker_daemon": ("models", {"sub": "运行监控"}),
    "concurrency_risk": ("models", {"sub": "运行监控"}),
    "job_failed": ("generate", {}),
}


def emit(*, kind: str, title: str, body: str = "", level: str = "info",
         audience: str, dedupe_key: str = "",
         link_page: str = "", link_params: dict | None = None) -> int | None:
    """发一条通知。返回新通知的 id；因去重跳过时返回 None。

    `audience` 三选一：`user:<id>` / `project:<id>` / `config_admins`。
    拼错不会被静默忽略——格式不对直接抛，因为一条发错人的通知比没发更糟。
    """
    if kind not in KINDS:
        raise ValueError(f"不认识的 kind：{kind}（认的只有 {KINDS}）")
    if level not in LEVELS:
        raise ValueError(f"不认识的 level：{level}")
    if not _valid_audience(audience):
        raise ValueError(f"audience 格式不对：{audience!r}"
                         f"（应为 user:<id> / project:<id> / config_admins）")

    page, params = DEFAULT_LINKS.get(kind, ("", {}))
    if link_page:
        page = link_page
    if link_params is not None:
        params = link_params

    with get_conn() as conn:
        # 去重：同一 (kind, audience, dedupe_key) 已有**未结**条目就不再发。
        # "未结"的定义＝还没人把它标记成已解决；这里用 dedupe_key 本身做判据——
        # 有 dedupe_key 就按它去重，没有就每次都发。
        if dedupe_key:
            row = conn.execute(
                """SELECT id FROM notification
                    WHERE kind = ? AND audience = ? AND dedupe_key = ?
                    ORDER BY id DESC LIMIT 1""",
                (kind, audience, dedupe_key)).fetchone()
            if row is not None:
                return None
        cur = conn.execute(
            """INSERT INTO notification
                   (kind, level, title, body, link_page, link_params,
                    audience, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, level, title[:200], body[:2000], page,
             json.dumps(params, ensure_ascii=False, sort_keys=True),
             audience, dedupe_key[:120]))
        return int(cur.lastrowid)


def resolve(*, kind: str, audience: str, dedupe_key: str) -> int:
    """把同类未结通知标成"已解决"——删掉它们，让下一次真的能再发。

    为什么要删而不是加一个 resolved 列：这些是**过程性提醒**（worker 掉了），
    不是审计记录。留着会让人在"已恢复"之后还看到一堆过时的红条。
    真要审计，`config_change_log` 那一层才是干这个的。
    """
    with get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM notification
                WHERE kind = ? AND audience = ? AND dedupe_key = ?""",
            (kind, audience, dedupe_key))
        return cur.rowcount


# 通知保留天数。为什么要有上限：这张表**只增不减**——`worker_daemon` 类
# 每次补起/失败各记一条，worker 每天崩 5 次就是 1825 条/年；而收件箱只取最近
# 30 条，留下的旧行既没人看、又让 COUNT(*) 未读越来越慢。
# 7 天与 `worker_node` 的 prune 同一个口径（lease.py 那条 DELETE），
# 运维上「一周前的通知还值不值得看」有一个统一答案。
NOTIFICATION_KEEP_DAYS = 7

# 上次清理的时间（monotonic 秒）。**不每轮都删**：`worker_node` 那条 prune
# 就是为同样的事被优化过（心跳 2 秒一次、每小时才清一遍），这里同一条道理——
# 清理要清的是"7 天前"，每 15 秒跑一遍永远是 0 行，等于拿写锁空转。
_last_prune = 0.0


def prune(force: bool = False) -> int:
    """删掉超过保留期的通知。返回删了几条。

    **已读表靠外键 CASCADE 跟着删**（0049 建表时写的 `ON DELETE CASCADE`），
    所以不用手动清 `notification_read`。
    """
    global _last_prune
    import time
    now = time.monotonic()
    if not force and now - _last_prune < 3600:
        return 0
    _last_prune = now
    try:
        with get_conn() as conn:
            cur = conn.execute(
                f"""DELETE FROM notification
                     WHERE created_at < datetime('now','localtime','-{NOTIFICATION_KEEP_DAYS} days')"""
            )
            return cur.rowcount
    except Exception:
        return 0


def _valid_audience(audience: str) -> bool:
    if audience == "config_admins":
        return True
    for prefix in ("user:", "project:"):
        if audience.startswith(prefix):
            rest = audience[len(prefix):]
            return rest.isdigit() and int(rest) > 0
    return False


def _user_id(user) -> int:
    """从 `user` 里取 id。**deps.require_user 返回的是 sqlite3.Row**，不是 dict——
    用 `isinstance(user, dict)` 判会在真实请求里全部落空（收件箱永远是空），
    而单测里传 dict 又是好的。两种都认，才不会出现"测试绿、界面空"。
    """
    if user is None:
        return 0
    try:
        value = user["id"]
    except (KeyError, IndexError, TypeError):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def inbox(user, *, limit: int = 30, unread_only: bool = False) -> dict:
    """这个人该看的通知 + 未读数。

    `user` 是 `deps.require_user` 给的那一份 dict；`is_superuser` 或
    `system:config` 决定他能不能看到 `config_admins` 那类。
    """
    uid = _user_id(user)
    if not uid:
        return {"items": [], "unread": 0}
    limit = max(1, min(int(limit), 100))

    # 这个人的可见范围：点名的 + 所在项目的 + （有权限时）管理员类。
    audience_clauses = ["n.audience = ?"]
    params: list = [f"user:{uid}"]
    project_ids = _my_project_ids(uid)
    for pid in project_ids:
        audience_clauses.append("n.audience = ?")
        params.append(f"project:{pid}")
    if _is_config_admin(user):
        audience_clauses.append("n.audience = 'config_admins'")
    where = "(" + " OR ".join(audience_clauses) + ")"

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT n.*, (r.user_id IS NOT NULL) AS is_read
                  FROM notification n
             LEFT JOIN notification_read r
                    ON r.notification_id = n.id AND r.user_id = ?
                 WHERE {where}
                 {"AND r.user_id IS NULL" if unread_only else ""}
              ORDER BY n.id DESC LIMIT ?""",
            [uid] + params + [limit]).fetchall()
        unread = conn.execute(
            f"""SELECT COUNT(*) AS n
                  FROM notification n
             LEFT JOIN notification_read r
                    ON r.notification_id = n.id AND r.user_id = ?
                 WHERE {where} AND r.user_id IS NULL""",
            [uid] + params).fetchone()["n"]

    items = []
    for row in rows:
        try:
            link_params = json.loads(row["link_params"] or "{}")
        except ValueError:
            link_params = {}
        items.append({
            "id": row["id"], "kind": row["kind"], "level": row["level"],
            "title": row["title"], "body": row["body"],
            "link_page": row["link_page"],
            "link_params": link_params if isinstance(link_params, dict) else {},
            "created_at": row["created_at"], "read": bool(row["is_read"]),
        })
    return {"items": items, "unread": int(unread)}


def _my_project_ids(uid: int) -> list[int]:
    """这个人拥有或参与的项目的 id。管理员不走这条（见 `inbox` 的
    `config_admins` 判定）——但项目类通知对管理员也该可见，所以这里不判管理员。"""
    try:
        with get_conn() as conn:
            # UNION 两边的列名不同，显式起别名成同一个名字，否则取值要靠猜。
            rows = conn.execute(
                """SELECT id FROM project WHERE owner_id = ?
                   UNION
                   SELECT project_id AS id FROM project_member WHERE user_id = ?""",
                (uid, uid)).fetchall()
        return [int(r["id"]) for r in rows]
    except Exception:
        return []


def _is_config_admin(user) -> bool:
    """能不能看 `config_admins` 那类通知。判据与路由的 require_permission
    同一套：超管直接过，否则查 role_permission。"""
    try:
        if user["is_superuser"]:
            return True
    except (KeyError, IndexError, TypeError):
        pass
    try:
        # 项目里**没有** `has_permission` 这个函数（只有 `is_manager` 与
        # 路由层的 `require_permission`）。system:config 的实际人群就是
        # 管理员及以上——`models.js:140` 那几个页面挂的也是它。
        # 所以直接复用 `is_manager`，不另造一套判定。
        from access import is_manager
        return bool(is_manager(user))
    except Exception:
        return False


def mark_read(user, notification_ids: list[int]) -> int:
    """把若干条标记为已读。返回真的新标了几条（重复标不算）。"""
    uid = _user_id(user)
    if not uid:
        return 0
    ids = [int(i) for i in (notification_ids or []) if str(i).isdigit()]
    if not ids:
        return 0
    count = 0
    with get_conn() as conn:
        for nid in ids:
            # INSERT OR IGNORE：同一条标两次不报错、也不重复计数
            cur = conn.execute(
                """INSERT OR IGNORE INTO notification_read
                       (notification_id, user_id) VALUES (?, ?)""",
                (nid, uid))
            count += cur.rowcount
    return count


def mark_all_read(user) -> int:
    """把这个人当前看得见的未读全标已读。"""
    payload = inbox(user, limit=100, unread_only=True)
    return mark_read(user, [item["id"] for item in payload["items"]])
