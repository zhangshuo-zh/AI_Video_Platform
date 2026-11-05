"""文档元数据与编辑锁的读写。

正文不在这里——那是 workspace.py 的事。这一层只回答四个问题：
这份文档归哪个项目、到哪一步了、当前对应哪次提交、现在谁在改。

锁的过期不靠定时任务清理（规格 02 §6.1「锁已过期允许回收」）：
读的时候按 `expires_at > now` 判活，抢的时候直接覆盖过期行。
2—5 个人、锁 15 分钟，过期行攒不下几行，为此起一个后台线程不值得。
"""
from __future__ import annotations

import sqlite3

from db import get_conn
from repositories.projects import touch
from repositories.reviews import reviews

# 一把锁默认多久（分钟）由 config.LOCK_MINUTES 给，调用方传进来，
# 仓储层不 import config：这样"锁多久"这件事在测试里可以随便调，不用改环境变量重启进程。
_LOCK_ALIVE = "expires_at > datetime('now','localtime')"

_SELECT = """
    SELECT d.*,
           COALESCE(e.seq, 0)    AS episode_seq,
           COALESCE(e.title, '') AS episode_title,
           l.holder_user_id, l.expires_at,
           COALESCE(u.display_name, '') AS holder_name
      FROM document d
      -- 锁只在"还没过期"时才 JOIN 得上。写进 ON 而不是 WHERE：
      -- WHERE 会把没锁的文档整行也过滤掉，ON 里过滤只让锁那几列变成 NULL。
      LEFT JOIN doc_lock l ON l.doc_id = d.id
                           AND l.expires_at > datetime('now','localtime')
      LEFT JOIN app_user u ON u.id = l.holder_user_id
      LEFT JOIN episode e ON e.id = d.episode_id
"""


class DocumentRepository:
    def list_by_projects(self, project_ids: list[int]) -> list[sqlite3.Row]:
        """一次读回一组项目的文档（首页工作台用）。

        存在的理由就是"别按项目逐个回库"：首页列的是全部在产项目，
        逐项目读文档＋签字在 25 个项目那一档量到回库 159 次。
        """
        ids = sorted({int(i) for i in project_ids})
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            return conn.execute(
                _SELECT + f" WHERE d.project_id IN ({marks})"
                " ORDER BY d.kind, (d.episode_id IS NULL), e.seq, d.title",
                ids).fetchall()

    def list_by_project(self, project_id: int, kind: str | None = None,
                        episode_id: int | None = None) -> list[sqlite3.Row]:
        conds = ["d.project_id = ?"]
        args: list = [project_id]
        if kind:
            conds.append("d.kind = ?")
            args.append(kind)
        if episode_id:
            conds.append("d.episode_id = ?")
            args.append(episode_id)
        with get_conn() as conn:
            return conn.execute(
                _SELECT + " WHERE " + " AND ".join(conds)
                # 排序按"类型 → 没挂靠的（总纲那一类）→ 集序 → 标题"。
                # 这里必须写 e.seq 而不是 SELECT 里那个 episode_seq：
                # 别名带上表前缀（d.episode_seq）SQLite 就查不到这一列。
                + " ORDER BY d.kind, (e.seq IS NOT NULL), e.seq, d.title, d.id",
                args,
            ).fetchall()

    def get(self, doc_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(_SELECT + " WHERE d.id = ?", (doc_id,)).fetchone()

    def path_taken(self, rel_path: str) -> bool:
        """路径有没有被占。给 workspace.plan_rel_path 当 `taken` 回调。"""
        with get_conn() as conn:
            return conn.execute(
                "SELECT 1 FROM document WHERE rel_path = ?", (rel_path,)
            ).fetchone() is not None

    def create(self, project_id: int, episode_id: int | None, kind: str,
               title: str, rel_path: str, git_commit: str | None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO document (project_id, episode_id, kind, title, rel_path, git_commit)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project_id, episode_id, kind, title, rel_path, git_commit),
            )
            # 建一份文档就是往项目里加了东西，"最后修改"该跟着走（v0.62 那一列的语义）
            touch(conn, "project", project_id)
            return conn.execute(
                _SELECT + " WHERE d.id = ?", (cursor.lastrowid,)
            ).fetchone()

    def save(self, doc_id: int, git_commit: str) -> sqlite3.Row | None:
        """一次正文保存落到库上的那笔：版本指针 + 时间 + 项目"最后修改"。

        三件事在一个事务里，不会出现"版本号更新了但项目看着没动过"。
        """
        with get_conn() as conn:
            row = conn.execute("SELECT project_id FROM document WHERE id = ?",
                               (doc_id,)).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE document SET git_commit = ?,
                          updated_at = datetime('now','localtime') WHERE id = ?""",
                (git_commit, doc_id),
            )
            touch(conn, "project", row["project_id"])
            return conn.execute(
                _SELECT + " WHERE d.id = ?", (doc_id,)
            ).fetchone()

    def set_status(self, doc_id: int, status: str) -> sqlite3.Row | None:
        """草稿 → 润色 → 定稿。取值白名单在路由层（Literal 会先挡住）。"""
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM document WHERE id = ?", (doc_id,)).fetchone() is None:
                return None
            conn.execute(
                """UPDATE document SET status = ?, updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (status, doc_id),
            )
            row = conn.execute("SELECT project_id FROM document WHERE id = ?",
                               (doc_id,)).fetchone()
            touch(conn, "project", row["project_id"])
            return conn.execute(_SELECT + " WHERE d.id = ?", (doc_id,)).fetchone()

    def update_meta(self, doc_id: int, title: str,
                    episode_id: int | None) -> sqlite3.Row | None:
        """改标题与挂靠的分集。**路径不动**：

        rel_path 是这条正文在 Git 里的身份，改名就挪文件的话，
        历史会断成两个文件（git 按路径记历史），"这一版改了什么"就查不到了。
        给人看的名字在 title 列，不在文件名。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM document WHERE id = ?", (doc_id,)).fetchone() is None:
                return None
            conn.execute(
                """UPDATE document SET title = ?, episode_id = ?,
                          updated_at = datetime('now','localtime') WHERE id = ?""",
                (title, episode_id, doc_id),
            )
            row = conn.execute("SELECT project_id FROM document WHERE id = ?",
                               (doc_id,)).fetchone()
            touch(conn, "project", row["project_id"])
            return conn.execute(_SELECT + " WHERE d.id = ?", (doc_id,)).fetchone()

    def delete(self, doc_id: int) -> sqlite3.Row | None:
        """删元数据行。锁行跟着外键 CASCADE 走，不用手动清。

        签字要手动清：0025 把 `review_item.doc_id` 换成了多态的
        `target_kind + target_id`，多态列建不了外键，也就没有 CASCADE 白送了。
        两句在同一个 `with get_conn()` 里，要么都成要么都不成——
        文档删了签字还在，就成了指向空气的审核记录。

        返回删掉的那一行（含 rel_path），因为调用方要拿它去删文件——
        删完再查就什么都没有了，文件会变成孤儿。
        """
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM document WHERE id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                return None
            project_id = row["project_id"]
            reviews.delete_for_document(conn, doc_id)
            conn.execute("DELETE FROM document WHERE id = ?", (doc_id,))
            touch(conn, "project", project_id)
            return row

    def count_by_project(self, project_id: int) -> int:
        """删项目前给人看清楚会带走几份文档（元数据；正文留在 Git 历史里）。"""
        with get_conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM document WHERE project_id = ?", (project_id,)
            ).fetchone()["n"]

    def paths_by_project(self, project_id: int) -> list[str]:
        """删项目时要从工作区摘掉的那些正文路径。

        只按 `project_id = ?` 取，所以拿到的一定是这个项目名下的路径——
        路径本身带 `p<id>/` 前缀是第二重保险，真正的授权依据是这一行查询。
        """
        with get_conn() as conn:
            return [r["rel_path"] for r in conn.execute(
                "SELECT rel_path FROM document WHERE project_id = ?", (project_id,)
            ).fetchall()]

    def paths_by_episode(self, episode_id: int) -> list[str]:
        """删某一集时要从工作区摘掉的那些正文路径（口径同 paths_by_project）。

        必须有这一条：`document.episode_id` 是 `ON DELETE CASCADE`（0013），
        删集的 SQL 会把文档行带走，但**文件不在数据库里**——没有这份清单，
        删完集工作区就剩下几份没人认领的 .md，而它们的路径已经查不回来了。
        全项目共用的那几份（episode_id 为 NULL：总纲、人物小传）比不中，
        正好是"删一集不能删掉全项目稿件"的落点。
        """
        with get_conn() as conn:
            return [r["rel_path"] for r in conn.execute(
                "SELECT rel_path FROM document WHERE episode_id = ?", (episode_id,)
            ).fetchall()]

    # ---------- 编辑锁 ----------

    def lock_of(self, doc_id: int) -> sqlite3.Row | None:
        """当前**还有效**的那把锁。过期的一律当没有（回 None）。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT l.*, COALESCE(u.display_name, '') AS holder_name
                     FROM doc_lock l JOIN app_user u ON u.id = l.holder_user_id
                    WHERE l.doc_id = ? AND """ + _LOCK_ALIVE,
                (doc_id,),
            ).fetchone()

    def acquire_lock(self, doc_id: int, user_id: int, minutes: int) -> str:
        """拿锁。返回 `acquired` / `renewed` / `held`。

        自己已经拿着时是续期（刷新页面无所谓、心跳一次一次把 expires_at 推下去）；
        别人拿着且没过期时不动任何东西；过期行直接覆盖（这就是"超时回收"）。
        """
        with get_conn() as conn:
            current = conn.execute(
                "SELECT holder_user_id FROM doc_lock WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            alive = conn.execute(
                "SELECT 1 FROM doc_lock WHERE doc_id = ? AND " + _LOCK_ALIVE, (doc_id,)
            ).fetchone()
            if alive and current and current["holder_user_id"] != user_id:
                return "held"
            renewed = bool(current and current["holder_user_id"] == user_id)
            conn.execute(
                """INSERT INTO doc_lock (doc_id, holder_user_id, acquired_at, expires_at)
                   VALUES (?, ?, datetime('now','localtime'),
                           datetime('now','localtime', ?))
                   ON CONFLICT(doc_id) DO UPDATE SET
                       holder_user_id = excluded.holder_user_id,
                       acquired_at    = datetime('now','localtime'),
                       expires_at     = excluded.expires_at""",
                (doc_id, user_id, f"+{int(minutes)} minutes"),
            )
            return "renewed" if renewed else "acquired"

    def release_lock(self, doc_id: int, user_id: int) -> bool:
        """放锁。只有持锁人能放——别人的"我不编辑了"不能把你的锁清掉。

        管理员也不给强放（要抢就等它过期）：这一层做兜底是多余的，
        锁本来就是 15 分钟会自己松的东西。
        """
        with get_conn() as conn:
            return conn.execute(
                "DELETE FROM doc_lock WHERE doc_id = ? AND holder_user_id = ?",
                (doc_id, user_id),
            ).rowcount > 0

    def renew_on_save(self, doc_id: int, user_id: int, minutes: int) -> None:
        """保存成功时顺带续期（规格 02 §6.1「或延长」那一支）。

        只在**锁本来就在这个人手里**时续。不做"保存即抢锁"：
        那样等于绕过锁，两个人来回覆盖又回来了。
        """
        with get_conn() as conn:
            conn.execute(
                """UPDATE doc_lock SET expires_at = datetime('now','localtime', ?)
                    WHERE doc_id = ? AND holder_user_id = ? AND """ + _LOCK_ALIVE,
                (f"+{int(minutes)} minutes", doc_id, user_id),
            )

    def drop_expired(self) -> int:
        """清掉已过期的锁行，返回删了几行。

        正确性不依赖它（`lock_of` 已经按时间判活），留着是因为界面要显示
        "N 份文档正在被编辑"，攒着的死行会把计数虚高。启动时跑一次就够。
        """
        with get_conn() as conn:
            return conn.execute(
                "DELETE FROM doc_lock WHERE NOT (" + _LOCK_ALIVE + ")"
            ).rowcount


documents = DocumentRepository()
