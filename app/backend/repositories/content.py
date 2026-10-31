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

    def set_status(self, shot_id: int, status: str) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE shot SET status = ? WHERE id = ?", (status, shot_id))
            touch(conn, "shot", shot_id)

    def delete(self, shot_id: int) -> bool:
        with get_conn() as conn:
            touch(conn, "shot", shot_id)
            return conn.execute("DELETE FROM shot WHERE id = ?", (shot_id,)).rowcount > 0


class AssetRepository:
    def list_by_project(self, project_id: int, kind: str | None = None) -> list[sqlite3.Row]:
        with get_conn() as conn:
            if kind:
                return conn.execute(
                    "SELECT * FROM asset WHERE project_id = ? AND kind = ? ORDER BY kind, name",
                    (project_id, kind),
                ).fetchall()
            return conn.execute(
                "SELECT * FROM asset WHERE project_id = ? ORDER BY kind, name", (project_id,)
            ).fetchall()

    def create(self, project_id: int, kind: str, name: str, summary: str,
               consistency_prompt: str, ref_images: list[str]) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO asset
                   (project_id, kind, name, summary, consistency_prompt, ref_images)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project_id, kind, name, summary, consistency_prompt, json.dumps(ref_images)),
            )
            touch(conn, "project", project_id)
            return conn.execute("SELECT * FROM asset WHERE id = ?", (cursor.lastrowid,)).fetchone()

    def get(self, asset_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()

    def delete(self, asset_id: int) -> bool:
        with get_conn() as conn:
            touch(conn, "asset", asset_id)
            return conn.execute("DELETE FROM asset WHERE id = ?", (asset_id,)).rowcount > 0

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
            return conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()

    def remove_image(self, asset_id: int, relative_path: str) -> tuple[sqlite3.Row | None, bool]:
        """从 ref_images 里摘掉一条。返回 (资产行, 是否确实删掉了)。"""
        with get_conn() as conn:
            row = conn.execute("SELECT ref_images FROM asset WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                return None, False
            images = json.loads(row["ref_images"] or "[]")
            if relative_path not in images:
                return conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone(), False
            images.remove(relative_path)
            conn.execute(
                "UPDATE asset SET ref_images = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (json.dumps(images), asset_id),
            )
            touch(conn, "asset", asset_id)
            return conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone(), True

    def exists(self, project_id: int, kind: str, name: str) -> bool:
        with get_conn() as conn:
            return conn.execute(
                "SELECT 1 FROM asset WHERE project_id = ? AND kind = ? AND name = ?",
                (project_id, kind, name),
            ).fetchone() is not None


episodes = EpisodeRepository()
shots = ShotRepository()
assets = AssetRepository()
