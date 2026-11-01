"""数据访问层：SQL 只允许出现在这里。

换库（比如将来上 Postgres）时，改这一层就够；路由和模型不用动。
现在返回 sqlite3.Row，调用方用列名取值，所以方言差异被挡在里面。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn
from ranks import RANK_SQL

# 改了项目下面的任何东西，项目的"修改时间"就该跟着走。但层级不同、拿到
# project.id 的路径也不同，所以按定位方式给四句 UPDATE。写在一处是为了让
# 这句话只有一个出处；将来加 Job 表，就在这里加一条。
_TOUCH_SQL = {
    "project": "UPDATE project SET updated_at = datetime('now','localtime') WHERE id = ?",
    "episode": """UPDATE project SET updated_at = datetime('now','localtime')
                   WHERE id = (SELECT project_id FROM episode WHERE id = ?)""",
    # 分镜不直接挂项目，要经它所属的分集绕一层
    "shot": """UPDATE project SET updated_at = datetime('now','localtime')
                WHERE id = (SELECT e.project_id FROM shot s
                              JOIN episode e ON e.id = s.episode_id WHERE s.id = ?)""",
    "asset": """UPDATE project SET updated_at = datetime('now','localtime')
                 WHERE id = (SELECT project_id FROM asset WHERE id = ?)""",
}


def touch(conn, kind: str, target_id: int) -> None:
    """把项目的修改时间推到当前。

    用调用方事务里的 conn，而不是自己新开一个：这样它和业务写要么一起提交、
    要么一起回滚，不会出现"内容没改成但时间跳了"。
    删行的时候要在 DELETE 之前调用——行没了就来路径查不到了。
    """
    conn.execute(_TOUCH_SQL[kind], (target_id,))


class ProjectRepository:
    # 标签与计数在一条 SQL 里带出来：项目中心"内容"那一栏只是只读摘要，
    # 为它给每个项目再跑一趟接口就成了 N+1。
    # episode_done 是"已定稿几集"——进度条的数据源。集级状态这一层不用等生成
    # 队列（二期）就有，episode.status 建表时就在，缺的只是写入口。
    _SELECT = """
        SELECT p.*,
               COALESCE(o.display_name, '') AS owner_name,
               COALESCE((SELECT json_group_array(t.tag) FROM project_tag t
                          WHERE t.project_id = p.id), '[]') AS tags,
               (SELECT COUNT(*) FROM episode e WHERE e.project_id = p.id) AS episode_count,
               (SELECT COUNT(*) FROM episode e WHERE e.project_id = p.id
                  AND e.status = 'done') AS episode_done,
               (SELECT COUNT(*) FROM shot s JOIN episode e ON e.id = s.episode_id
                  WHERE e.project_id = p.id) AS shot_count,
               (SELECT COUNT(*) FROM asset a WHERE a.project_id = p.id) AS asset_count
          FROM project p
          LEFT JOIN app_user o ON o.id = p.owner_id
    """

    # 非管理员只看得见"自己名下"或"被邀请协作"的项目。过滤写在 SQL 里，
    # 不是捞出来再筛：项目一多，前端那层藏既慢又不可靠。
    _VISIBLE = """(p.owner_id = ?
               OR EXISTS (SELECT 1 FROM project_member m
                          WHERE m.project_id = p.id AND m.user_id = ?))"""

    # 归档是"收起来"不是"删掉"：行还在、还看得见，只是默认不挡在眼前。
    # 取值由 routers/projects.py 的白名单把关，库里没 CHECK。
    _STATUS_COND = {
        "active": "p.status = 'active'",
        "archived": "p.status = 'archived'",
        "all": "1",
    }

    # 排序按"最后打开"，不是"最后创建"：天天在几部剧之间来回跳时，
    # 我最近在哪和哪个最新建的是两回事。
    # 从没打开过的（last_opened_at 为 NULL）不剔除、沉底，再按 id 倒序——
    # 刚建完还没点过的项目不该因此在列表里消失。
    _ORDER = """ ORDER BY (p.last_opened_at IS NULL),
                          COALESCE(p.last_opened_at, '') DESC,
                          p.id DESC"""

    def list_visible(self, user_id: int, everything: bool,
                     status: str = "all") -> list[sqlite3.Row]:
        conds = [self._STATUS_COND.get(status, "1")]
        args: list = []
        if not everything:
            conds.append(self._VISIBLE)
            args += [user_id, user_id]
        with get_conn() as conn:
            return conn.execute(
                self._SELECT + " WHERE " + " AND ".join(conds) + self._ORDER, args
            ).fetchall()

    def get(self, project_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM project WHERE id = ?", (project_id,)
            ).fetchone()

    def create(self, name: str, description: str, tags: list[str],
               owner_id: int, member_ids: list[int] | None = None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO project (name, description, owner_id) VALUES (?, ?, ?)",
                (name, description, owner_id),
            )
            project_id = cursor.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO project_tag (project_id, tag) VALUES (?, ?)",
                [(project_id, tag) for tag in tags],
            )
            # 协作人和项目同一个事务：建项目时点选的人不能出现"项目建好了人没加上"
            conn.executemany(
                """INSERT OR IGNORE INTO project_member (project_id, user_id, added_by)
                   VALUES (?, ?, ?)""",
                [(project_id, uid, owner_id) for uid in (member_ids or [])],
            )
            return conn.execute(
                self._SELECT + " WHERE p.id = ?", (project_id,)
            ).fetchone()

    def set_tags(self, project_id: int, tags: list[str]) -> sqlite3.Row | None:
        """整组替换：先清空再插入，同一个事务里做完。"""
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
                return None
            conn.execute("DELETE FROM project_tag WHERE project_id = ?", (project_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO project_tag (project_id, tag) VALUES (?, ?)",
                [(project_id, tag) for tag in tags],
            )
            touch(conn, "project", project_id)
            return conn.execute(
                self._SELECT + " WHERE p.id = ?", (project_id,)
            ).fetchone()

    def update(self, project_id: int, name: str, description: str,
               tags: list[str]) -> sqlite3.Row | None:
        """名称／介绍／标签一次改完，同一个事务里做完。

        拆成三次写会有"名字改了标签没改"的半个状态，界面上看不出来是哪一步断的。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
                return None
            conn.execute(
                "UPDATE project SET name = ?, description = ? WHERE id = ?",
                (name, description, project_id),
            )
            conn.execute("DELETE FROM project_tag WHERE project_id = ?", (project_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO project_tag (project_id, tag) VALUES (?, ?)",
                [(project_id, tag) for tag in tags],
            )
            touch(conn, "project", project_id)
            return conn.execute(
                self._SELECT + " WHERE p.id = ?", (project_id,)
            ).fetchone()

    def set_status(self, project_id: int, status: str) -> sqlite3.Row | None:
        """归档／恢复。取值白名单在路由层，这里只管写。

        故意不 touch updated_at：那个字段的语义是"内容动过"，收进归档没动任何
        内容。两者混起来，项目中心的"最后修改"就再也不能用来判断哪部剧在推进。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
                return None
            conn.execute("UPDATE project SET status = ? WHERE id = ?", (status, project_id))
            return conn.execute(self._SELECT + " WHERE p.id = ?", (project_id,)).fetchone()

    def mark_opened(self, project_id: int) -> bool:
        """记一次"这个项目被打开过"。同上，不碰 updated_at。"""
        with get_conn() as conn:
            return conn.execute(
                "UPDATE project SET last_opened_at = datetime('now','localtime') WHERE id = ?",
                (project_id,),
            ).rowcount > 0

    def add_tags(self, project_id: int, tags: list[str], limit: int) -> sqlite3.Row | None:
        """追加标签，已有的跳过、总数超出 limit 就截掉后加的。

        和 set_tags 的"整组替换"是两回事：批量给十几个项目加同一个标签时，
        替换会把各项目自己原有的标签全冲掉。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM project WHERE id = ?", (project_id,)).fetchone() is None:
                return None
            have = [r["tag"] for r in conn.execute(
                "SELECT tag FROM project_tag WHERE project_id = ? ORDER BY rowid", (project_id,))]
            merged = list(have)
            for tag in tags:
                if tag not in merged:
                    merged.append(tag)
            conn.execute("DELETE FROM project_tag WHERE project_id = ?", (project_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO project_tag (project_id, tag) VALUES (?, ?)",
                [(project_id, tag) for tag in merged[:limit]],
            )
            touch(conn, "project", project_id)
            return conn.execute(self._SELECT + " WHERE p.id = ?", (project_id,)).fetchone()

    def all_tags(self, user_id: int, everything: bool) -> list[sqlite3.Row]:
        """筛选条用的标签清单，只统计自己看得见的项目。

        不筛的话，标签本身就会漏出"还有别的项目"——标签是项目信息的摘要。
        """
        where = "" if everything else """
             WHERE p.owner_id = :uid
                OR EXISTS (SELECT 1 FROM project_member m
                            WHERE m.project_id = p.id AND m.user_id = :uid)
        """
        with get_conn() as conn:
            return conn.execute(
                """SELECT t.tag, COUNT(*) AS n
                     FROM project_tag t JOIN project p ON p.id = t.project_id
                """ + where + """
                    GROUP BY t.tag ORDER BY n DESC, t.tag""",
                {"uid": user_id},
            ).fetchall()

    # ---------- 协作人与归属 ----------

    _MEMBER_SELECT = """
        SELECT m.user_id, u.username, u.display_name, m.created_at
          FROM project_member m JOIN app_user u ON u.id = m.user_id
    """

    def members(self, project_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                self._MEMBER_SELECT + " WHERE m.project_id = ? ORDER BY m.created_at, m.user_id",
                (project_id,),
            ).fetchall()

    def member_candidates(self, project_id: int, max_rank: int) -> list[sqlite3.Row]:
        """可被邀请的人：启用中、不是所有人、已在内的人不再列。

        层级过滤和账号列表用同一套规则（2 超管 > 1 管理员 > 0 其余），
        否则所有人可以把超级管理员拉进自己的项目里。
        """
        with get_conn() as conn:
            return conn.execute(
                """SELECT u.id AS user_id, u.username, u.display_name, '' AS created_at
                     FROM app_user u
                    WHERE u.is_active = 1
                      AND u.id <> (SELECT owner_id FROM project WHERE id = ?)
                      AND NOT EXISTS (SELECT 1 FROM project_member m
                                       WHERE m.project_id = ? AND m.user_id = u.id)
                      AND """ + RANK_SQL + """ <= ?
                    ORDER BY u.username""",
                (project_id, project_id, max_rank),
            ).fetchall()

    def candidate_users(self, max_rank: int, exclude_user_id: int) -> list[sqlite3.Row]:
        """建项目时挑协作人用：此时还没有项目，只按启用状态与层级过滤。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT u.id AS user_id, u.username, u.display_name, '' AS created_at
                     FROM app_user u
                    WHERE u.is_active = 1 AND u.id <> ?
                      AND """ + RANK_SQL + """ <= ?
                    ORDER BY u.username""",
                (exclude_user_id, max_rank),
            ).fetchall()

    def add_member(self, project_id: int, user_id: int, added_by: int) -> None:
        with get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO project_member (project_id, user_id, added_by)
                   VALUES (?, ?, ?)""",
                (project_id, user_id, added_by),
            )
            touch(conn, "project", project_id)

    def remove_member(self, project_id: int, user_id: int) -> None:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM project_member WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            )
            touch(conn, "project", project_id)

    def set_owner(self, project_id: int, owner_id: int) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE project SET owner_id = ? WHERE id = ?", (owner_id, project_id))
            touch(conn, "project", project_id)

    def transfer_owner(self, project_id: int, new_owner_id: int, old_owner_id: int) -> None:
        """换所有人，并把原所有人留在协作人名单里——**一个事务做完**。

        为什么不用 set_owner + add_member 拼：
        1. 那两个各自 touch `updated_at`。归属换了不等于内容动过，
           推了以后"最后修改"就不能用来看哪部剧在推进（和归档、打开同理）。
        2. 分两个事务的话，中间崩一次就是"项目没有所有人能管"或者
           "原所有人被踢出可见范围"——这种半截状态比报错难查得多。
        删账号那条流程仍然用 set_owner（它是"人在消失"，语义不同）。
        """
        with get_conn() as conn:
            conn.execute("UPDATE project SET owner_id = ? WHERE id = ?",
                         (new_owner_id, project_id))
            conn.execute(
                """INSERT OR IGNORE INTO project_member (project_id, user_id, added_by)
                   VALUES (?, ?, ?)""",
                (project_id, old_owner_id, old_owner_id),
            )
            # 新所有人本来在协作人名单里，交完他就多余了：删掉，
            # 不然名单上会出现"既是所有人又是协作人"，移除按钮还会对他开放
            conn.execute("DELETE FROM project_member WHERE project_id = ? AND user_id = ?",
                         (project_id, new_owner_id))

    def owned_by(self, user_id: int) -> list[sqlite3.Row]:
        """某人名下的项目，删账号时用来判断该转移还是一起删。"""
        with get_conn() as conn:
            return conn.execute(
                "SELECT id, name FROM project WHERE owner_id = ? ORDER BY id", (user_id,)
            ).fetchall()

    def summary(self, project_id: int) -> sqlite3.Row | None:
        """删除前的清点：会连带删掉多少东西。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT p.name,
                          (SELECT COUNT(*) FROM episode e WHERE e.project_id = p.id) AS episodes,
                          (SELECT COUNT(*) FROM shot s JOIN episode e ON e.id = s.episode_id
                            WHERE e.project_id = p.id) AS shots,
                          (SELECT COUNT(*) FROM asset a WHERE a.project_id = p.id) AS assets,
                          -- 文档只数元数据：正文会跟着走，但历史还在 Git 里（services 注释）
                          (SELECT COUNT(*) FROM document d WHERE d.project_id = p.id) AS documents
                     FROM project p WHERE p.id = ?""",
                (project_id,),
            ).fetchone()

    def asset_images(self, project_id: int) -> list[str]:
        """这个项目下所有资产的定妆图路径，删项目时连带清文件用。"""
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT ref_images FROM asset WHERE project_id = ?", (project_id,)
            ).fetchall()
        images: list[str] = []
        for row in rows:
            images.extend(json.loads(row["ref_images"] or "[]"))
        return images

    def delete(self, project_id: int) -> bool:
        # 外键是 ON DELETE CASCADE，分集、镜头、资产会一起走
        with get_conn() as conn:
            return conn.execute("DELETE FROM project WHERE id = ?", (project_id,)).rowcount > 0

    def count(self) -> int:
        with get_conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM project").fetchone()["n"]


# 模块级单例：路由直接 import 这个对象，不自己 new
projects = ProjectRepository()
