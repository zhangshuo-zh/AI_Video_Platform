"""创作单元、分镜、资产的读写。

三个实体放在一个文件里，因为它们总是一起被用：进一集 → 看它的镜头 → 镜头引用资产。
拆三个类没有意义，共用同样的连接与编号规则（序号都在事务里算）。
"""
import json
import sqlite3

from db import get_conn
from repositories.projects import touch


class EpisodeRepository:
    def list_by_project(self, project_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT e.*, (SELECT COUNT(*) FROM shot s WHERE s.episode_id = e.id) AS shot_count
                     FROM episode e WHERE e.project_id = ? ORDER BY e.seq""",
                (project_id,),
            ).fetchall()

    def get(self, episode_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()

    def create(self, project_id: int, title: str) -> sqlite3.Row:
        # 序号在事务里算，避免两个人同时建出现同号
        with get_conn() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM episode WHERE project_id = ?",
                (project_id,),
            ).fetchone()["n"]
            cursor = conn.execute(
                "INSERT INTO episode (project_id, seq, title) VALUES (?, ?, ?)",
                (project_id, seq, title),
            )
            touch(conn, "project", project_id)
            return conn.execute("SELECT * FROM episode WHERE id = ?", (cursor.lastrowid,)).fetchone()

    def set_status(self, episode_id: int, status: str) -> sqlite3.Row | None:
        """改分集状态（draft|active|done）。取值白名单在路由层。

        这一列 0004 建表时就有，但一直没人写得动它——项目中心要显示
        "已定稿 2/3 集"，缺的就是这个写入口。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM episode WHERE id = ?", (episode_id,)).fetchone() is None:
                return None
            conn.execute("UPDATE episode SET status = ? WHERE id = ?", (status, episode_id))
            touch(conn, "episode", episode_id)
            return conn.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()

    def delete(self, episode_id: int) -> bool:
        with get_conn() as conn:
            # 先推时间再删行：删完就查不到它属于哪个项目了
            touch(conn, "episode", episode_id)
            return conn.execute("DELETE FROM episode WHERE id = ?", (episode_id,)).rowcount > 0


class ShotRepository:
    def get(self, shot_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM shot WHERE id = ?", (shot_id,)).fetchone()

    def list_by_episode(self, episode_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM shot WHERE episode_id = ? ORDER BY seq", (episode_id,)
            ).fetchall()

    def create(self, episode_id: int, summary: str, prompt: str,
               duration: float, asset_ids: list[int]) -> sqlite3.Row:
        with get_conn() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM shot WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()["n"]
            cursor = conn.execute(
                """INSERT INTO shot (episode_id, seq, summary, prompt, duration, asset_ids)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (episode_id, seq, summary, prompt, duration, json.dumps(asset_ids)),
            )
            touch(conn, "episode", episode_id)
            return conn.execute("SELECT * FROM shot WHERE id = ?", (cursor.lastrowid,)).fetchone()

    def update(self, shot_id: int, summary: str, prompt: str,
               duration: float, asset_ids: list[int]) -> sqlite3.Row | None:
        """整体改一镜的四个可编辑字段。

        **不动 episode_id 与 seq**：镜头属于集、镜号是这一集里的顺序，
        搬动它等于让"第 3 镜"在别的集里又出现一次；真要挪就删了重建。
        改完 touch 项目时间：项目中心"最近修改"是这件事在界面上的唯一回执。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM shot WHERE id = ?", (shot_id,)).fetchone() is None:
                return None
            conn.execute(
                """UPDATE shot SET summary = ?, prompt = ?, duration = ?, asset_ids = ?
                    WHERE id = ?""",
                (summary, prompt, duration, json.dumps(asset_ids), shot_id),
            )
            touch(conn, "shot", shot_id)
            return conn.execute("SELECT * FROM shot WHERE id = ?", (shot_id,)).fetchone()

    def set_status(self, shot_id: int, status: str) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE shot SET status = ? WHERE id = ?", (status, shot_id))
            touch(conn, "shot", shot_id)

    def delete(self, shot_id: int) -> bool:
        with get_conn() as conn:
            touch(conn, "shot", shot_id)
            return conn.execute("DELETE FROM shot WHERE id = ?", (shot_id,)).rowcount > 0


class AssetRepository:
    """资产。全局资产（project_id 为 NULL）与项目资产共用这一张表、这一套读写。

    两个数在列表 SQL 里一次带出来，不给每行再跑一趟接口：
    `tags` 走 asset_tag 聚合，`ref_shot_count` 走 json_each 反查——
    卡片要显示"关联几镜"，而引用关系本来就存在 shot.asset_ids 那个 JSON 数组里。
    """

    _SELECT = """
        SELECT a.*,
               COALESCE((SELECT json_group_array(t.tag) FROM asset_tag t
                          WHERE t.asset_id = a.id), '[]') AS tags,
               (SELECT COUNT(*) FROM shot s, json_each(s.asset_ids) j
                 WHERE json_valid(s.asset_ids) AND j.value = a.id) AS ref_shot_count
          FROM asset a
    """

    def list_visible(self, project_id: int, kind: str | None = None,
                     scope: str = "all") -> list[sqlite3.Row]:
        """当前项目能看见的资产：自有 ＋ 全局。scope 决定要不要其中一半。

        默认 all 是指令 §3.1-C 的"范围筛选"缺省档；`project` 只看自有、
        `global` 只看全局池。
        """
        clauses, params = [], []
        if scope == "project":
            clauses.append("a.project_id = ?")
            params.append(project_id)
        elif scope == "global":
            clauses.append("a.project_id IS NULL")
        else:
            clauses.append("(a.project_id = ? OR a.project_id IS NULL)")
            params.append(project_id)
        if kind:
            clauses.append("a.kind = ?")
            params.append(kind)
        with get_conn() as conn:
            rows = conn.execute(
                f"{self._SELECT} WHERE {' AND '.join(clauses)}"
                " ORDER BY (a.project_id IS NULL), a.kind, a.name",
                params,
            ).fetchall()
        return rows

    def get(self, asset_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(f"{self._SELECT} WHERE a.id = ?", (asset_id,)).fetchone()

    def usable_ids(self, project_id: int, asset_ids: list[int]) -> set[int]:
        """这一组 id 里，哪些是当前项目**可以引用**的：自有或全局。

        只回 id 不回整行：镜头保存时只需要判断"这组引用成不成立"。
        把别家项目的资产 id 挡在写入口，才能保证那张引用表不会出现
        "项目 A 的镜头引用了项目 B 的角色"——界面上看不见，删除保护也算不到它。
        """
        ids = [int(i) for i in asset_ids]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT id FROM asset
                     WHERE id IN ({marks}) AND (project_id = ? OR project_id IS NULL)""",
                [*ids, project_id],
            ).fetchall()
        return {row["id"] for row in rows}

    def exists_name(self, project_id: int | None, kind: str, name: str,
                    exclude_id: int | None = None) -> bool:
        """同归属范围、同类型内查重。

        `project_id IS ?` 而不是 `= ?`：全局资产的范围是 NULL，用 `=` 永远比不中，
        重名就漏了——这正是 0012 要把唯一索引拆成两条的同一个原因。
        """
        sql = "SELECT 1 FROM asset WHERE kind = ? AND name = ?"
        params: list = [kind, name]
        if project_id is None:
            sql += " AND project_id IS NULL"
        else:
            sql += " AND project_id = ?"
            params.append(project_id)
        if exclude_id is not None:
            sql += " AND id <> ?"
            params.append(exclude_id)
        with get_conn() as conn:
            return conn.execute(sql, params).fetchone() is not None

    def create(self, project_id: int | None, kind: str, name: str, summary: str,
               consistency_prompt: str, aliases: list[str] | None = None,
               tags: list[str] | None = None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO asset
                   (project_id, kind, name, aliases, summary, consistency_prompt)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project_id, kind, name, json.dumps(aliases or [], ensure_ascii=False),
                 summary, consistency_prompt),
            )
            asset_id = cursor.lastrowid
            conn.executemany("INSERT OR IGNORE INTO asset_tag (asset_id, tag) VALUES (?, ?)",
                             [(asset_id, tag) for tag in tags or []])
            # 全局资产不属于任何项目，自然不该推动哪个项目的"最后修改"
            if project_id is not None:
                touch(conn, "asset", asset_id)
            return conn.execute(f"{self._SELECT} WHERE a.id = ?", (asset_id,)).fetchone()

    def update(self, asset_id: int, *, kind: str, name: str, summary: str,
               consistency_prompt: str, aliases: list[str], tags: list[str],
               project_id: int | None) -> sqlite3.Row | None:
        """整体提交：名称／类型／简介／提示词／别名／标签／归属范围一次改完。

        同一个字段不留两个入口（指令 §4.1），所以这里也不接受部分更新。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM asset WHERE id = ?", (asset_id,)).fetchone() is None:
                return None
            conn.execute(
                """UPDATE asset SET kind = ?, name = ?, summary = ?, consistency_prompt = ?,
                          aliases = ?, project_id = ?,
                          updated_at = datetime('now','localtime')
                   WHERE id = ?""",
                (kind, name, summary, consistency_prompt,
                 json.dumps(aliases, ensure_ascii=False), project_id, asset_id),
            )
            conn.execute("DELETE FROM asset_tag WHERE asset_id = ?", (asset_id,))
            conn.executemany("INSERT OR IGNORE INTO asset_tag (asset_id, tag) VALUES (?, ?)",
                             [(asset_id, tag) for tag in tags])
            touch(conn, "asset", asset_id)
            return conn.execute(f"{self._SELECT} WHERE a.id = ?", (asset_id,)).fetchone()

    def set_cover(self, asset_id: int, path: str) -> tuple[sqlite3.Row | None, bool]:
        """把某张已登记的图挪到数组第一位＝封面。

        不加 is_cover 列：封面本来就是"第一张参考图"这个语义，
        多一个布尔就多一个能与数组顺序不一致的真相源。
        """
        with get_conn() as conn:
            row = conn.execute("SELECT ref_images FROM asset WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                return None, False
            images = json.loads(row["ref_images"] or "[]")
            if path not in images:
                return self.get(asset_id), False
            images.remove(path)
            images.insert(0, path)
            conn.execute(
                "UPDATE asset SET ref_images = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (json.dumps(images), asset_id),
            )
            touch(conn, "asset", asset_id)
            return conn.execute(f"{self._SELECT} WHERE a.id = ?", (asset_id,)).fetchone(), True

    def add_image(self, asset_id: int, relative_path: str) -> sqlite3.Row | None:
        """把一张图的相对路径追加进 ref_images。

        读-改-写放在同一个事务里：两个人同时传，不会互相覆盖。
        """
        with get_conn() as conn:
            row = conn.execute("SELECT ref_images FROM asset WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                return None
            images = json.loads(row["ref_images"] or "[]")
            images.append(relative_path)
            conn.execute(
                "UPDATE asset SET ref_images = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (json.dumps(images), asset_id),
            )
            touch(conn, "asset", asset_id)
            return conn.execute(f"{self._SELECT} WHERE a.id = ?", (asset_id,)).fetchone()

    def remove_image(self, asset_id: int, relative_path: str) -> tuple[sqlite3.Row | None, bool]:
        """从 ref_images 里摘掉一条。返回 (资产行, 是否确实删掉了)。"""
        with get_conn() as conn:
            row = conn.execute("SELECT ref_images FROM asset WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                return None, False
            images = json.loads(row["ref_images"] or "[]")
            if relative_path not in images:
                return self.get(asset_id), False
            images.remove(relative_path)
            conn.execute(
                "UPDATE asset SET ref_images = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (json.dumps(images), asset_id),
            )
            touch(conn, "asset", asset_id)
            return conn.execute(f"{self._SELECT} WHERE a.id = ?", (asset_id,)).fetchone(), True

    def delete(self, asset_id: int) -> bool:
        with get_conn() as conn:
            touch(conn, "asset", asset_id)
            # asset_tag 有 ON DELETE CASCADE，标签行跟着走，不留孤儿
            return conn.execute("DELETE FROM asset WHERE id = ?", (asset_id,)).rowcount > 0

    def referencing_shots(self, asset_id: int) -> list[sqlite3.Row]:
        """哪些镜头引用了它。跨项目查得到，所以调用方必须再按项目权限过滤一遍。

        走 json_each 而不是 LIKE '%"12"%'：后者会把 12 匹配进 112／120，
        引用关系错了比没有更糟。
        """
        with get_conn() as conn:
            return conn.execute(
                """SELECT s.id AS shot_id, s.seq, s.summary, e.id AS episode_id,
                          e.seq AS episode_seq, e.title AS episode_title,
                          e.project_id AS project_id, p.name AS project_name
                     FROM shot s
                     JOIN episode e ON e.id = s.episode_id
                     JOIN project p ON p.id = e.project_id
                     JOIN json_each(s.asset_ids) j ON j.value = ?
                    WHERE json_valid(s.asset_ids)
                    ORDER BY p.name, e.seq, s.seq""",
                (asset_id,),
            ).fetchall()


episodes = EpisodeRepository()
shots = ShotRepository()
assets = AssetRepository()
