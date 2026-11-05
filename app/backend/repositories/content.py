"""创作单元、分镜、资产，以及资产的两张附属表（人物档案、命名形态）的读写。

这几个实体放在一个文件里，因为它们总是一起被用：进一集 → 看它的镜头 → 镜头引用资产；
而档案与形态只在"看这条资产"的时候才出现，离开资产没有独立意义。
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
               duration: float, asset_ids: list[int],
               variant_ids: list[int] | None = None,
               *, shot_size: str = "", camera_move: str = "", aspect: str = "",
               style: str = "", negative_prompt: str = "") -> sqlite3.Row:
        with get_conn() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM shot WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()["n"]
            cursor = conn.execute(
                """INSERT INTO shot (episode_id, seq, summary, prompt, duration,
                                     asset_ids, variant_ids,
                                     shot_size, camera_move, aspect, style,
                                     negative_prompt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (episode_id, seq, summary, prompt, duration, json.dumps(asset_ids),
                 json.dumps(variant_ids or []), shot_size, camera_move, aspect,
                 style, negative_prompt),
            )
            touch(conn, "episode", episode_id)
            return conn.execute("SELECT * FROM shot WHERE id = ?", (cursor.lastrowid,)).fetchone()

    def update(self, shot_id: int, summary: str, prompt: str,
               duration: float, asset_ids: list[int],
               variant_ids: list[int] | None = None,
               *, shot_size: str = "", camera_move: str = "", aspect: str = "",
               style: str = "", negative_prompt: str = "") -> sqlite3.Row | None:
        """整体改一镜的可编辑字段（0034 起加形态引用，0037 起加镜头设定五栏）。

        **不动 episode_id 与 seq**：镜头属于集、镜号是这一集里的顺序，
        搬动它等于让"第 3 镜"在别的集里又出现一次；真要挪就删了重建。
        改完 touch 项目时间：项目中心"最近修改"是这件事在界面上的唯一回执。

        五栏设定走关键字参数而不是位置参数：老调用方（含测试夹具）传四个位置参数
        照样能跑，不会因为多了一栏就得全仓改一遍。
        """
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM shot WHERE id = ?", (shot_id,)).fetchone() is None:
                return None
            conn.execute(
                """UPDATE shot SET summary = ?, prompt = ?, duration = ?, asset_ids = ?,
                          variant_ids = ?, shot_size = ?, camera_move = ?, aspect = ?,
                          style = ?, negative_prompt = ?, revision = revision + 1,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (summary, prompt, duration, json.dumps(asset_ids),
                 json.dumps(variant_ids or []), shot_size, camera_move, aspect,
                 style, negative_prompt, shot_id),
            )
            touch(conn, "shot", shot_id)
            return conn.execute("SELECT * FROM shot WHERE id = ?", (shot_id,)).fetchone()

    # 没有 set_status：`shot.status` 那一列在 0023 被摘掉了——它零调用方，
    # 而"这条镜头生成过没有"的事实本来就在 `job` 表里（最新一条作业的状态）。
    # 再加一列就是两个真相源，早晚对不上。

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
        SELECT a.*, COALESCE(tg.tags, '[]') AS tags, COALESCE(ct.n, 0) AS ref_shot_count
          FROM asset a
          LEFT JOIN (SELECT asset_id AS aid, json_group_array(tag ORDER BY tag) AS tags
                       FROM asset_tag GROUP BY asset_id) tg ON tg.aid = a.id
          LEFT JOIN (SELECT CAST(j.value AS INTEGER) AS aid, COUNT(*) AS n
                       FROM shot s, json_each(s.asset_ids) j
                      WHERE json_valid(s.asset_ids) AND j.value = CAST(j.value AS INTEGER)
                      GROUP BY CAST(j.value AS INTEGER)) ct ON ct.aid = a.id
    """
    # ^ 2026-11-04 从两个**相关子查询**改成两个派生表 LEFT JOIN。原来那份每读一行资产
    # 就把 shot×json_each 整个重扫一遍：列表一次请求是 O(资产数×镜头数)，实测 600 资产／
    # 1500 镜头 180ms → 现在 13ms，而这句 SQL 被列表、`get()`、改、封面、加图删图共八处共用。
    # 两处语义都是**造数据逐行比对过**才换的，而且第一版就是被比对打回来的：
    # - `ref_shot_count` 原来写 `j.value = a.id`，靠 `a.id` 的 INTEGER 亲和把
    #   `'["12"]'` 里的**字符串** 12 也并进来了；直接 `GROUP BY j.value` 会把 12 与 '12'
    #   分成两组，LEFT JOIN 于是**一行资产匹配两遍**（600 行变 970 行、计数还少算）。
    #   现在连接键先 `CAST(... AS INTEGER)` 归一，并且只留 `j.value = CAST(j.value AS INTEGER)`
    #   成立的值——这条判据与原式同形（对任意整数 id，"值等于它"当且仅当"值等于自己的整数形式"），
    #   所以 `'abc'`、JSON null 这些照旧不算，数字与字符串两种写法算得一样。
    #   `test_assets.py` 里有一条用例专门摆这两种写法。
    # - `tags` 原来靠 `asset_tag` 的主键 (asset_id, tag) 天然按标签字典序扫出来，
    #   聚合成派生表后扫描顺序不再有保障，所以把顺序写进聚合（`ORDER BY tag`）。
    # 只读指纹与预检那几列的路径走 `_PLAIN_COLUMNS`，不背这两张派生表。

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

    # 指纹与预检只读这几列。用 `self._SELECT` 会顺手把 `tags` 与 `ref_shot_count`
    # 两个相关子查询也跑一遍——那是资产中心卡片要显示的数，分镜列表与生产前检查
    # 根本不碰，为一个不看的数字给每个资产多打两次 shot 表扫描不值当。
    _PLAIN_COLUMNS = ("id", "name", "kind", "summary", "consistency_prompt",
                      "ref_images", "updated_at")

    def get_many(self, asset_ids: list[int] | set[int]) -> dict[int, sqlite3.Row]:
        """一次读回一组资产（含全局资产），键是 id。

        存在的理由就是一句 SQL 打完一遍，而不是循环里逐个 `get()`：
        一集 13 镜、每镜两三个引用，逐资产回库就是三四十次；生产前检查与
        分镜「源已变」都要读同一批资产，所以两处共用这一个出处。
        查不到的 id 不会出现在返回的字典里——调用方本来就要区分"资产被删了"。
        """
        ids = sorted({int(i) for i in asset_ids})
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        columns = ", ".join(f"a.{c}" for c in self._PLAIN_COLUMNS)
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM asset a WHERE a.id IN ({marks})", ids).fetchall()
        return {int(row["id"]): row for row in rows}

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


def _images_of(row) -> list[str]:
    """`ref_images` 那一列的 JSON 文本 → 路径清单。坏数据当空清单，不猜。

    和 v0.96 那条「资产没有参考图」的判空缺陷同一个教训：`"[]"` 是非空字符串，
    `if not row["ref_images"]` 永远不成立，必须解析之后再看长度。
    """
    try:
        raw = json.loads(row["ref_images"] or "[]")
    except (TypeError, ValueError):
        return []
    return [x for x in raw if isinstance(x, str) and x]


class CharacterProfileRepository:
    """人物的文字档案：履历／性格／成长过程／文字设定提示词。

    一个资产最多一份，主键就是 `asset_id`——多一个自增 id 就多一个"同一人物两份档案"
    的可能。读不到行回一份空档案：空档案是"还没写"，不是"缺数据"，所以老人物升级后
    照样打得开，调用方不必为它编一条 404。

    `text_prompt` 与 `asset.consistency_prompt` 必须一直是两列：前者是"让文本模型
    怎么写这份设定"的输入，后者是"让图像模型画稳定"的输入。合成一列的结果是
    改一次履历就把定妆图的依据也改了，而这两件事由不同的人在不同的页上做。
    """

    FIELDS = ("biography", "personality", "growth_arc", "text_prompt")

    def get(self, asset_id: int) -> dict:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM asset_character_profile WHERE asset_id = ?",
                               (asset_id,)).fetchone()
        if row is None:
            out = {"asset_id": asset_id, "exists": False, "created_at": "", "updated_at": ""}
            out.update({k: "" for k in self.FIELDS})
            return out
        out = {"asset_id": asset_id, "exists": True,
               "created_at": row["created_at"], "updated_at": row["updated_at"]}
        out.update({k: row[k] for k in self.FIELDS})
        return out

    def upsert(self, asset_id: int, *, biography: str, personality: str,
               growth_arc: str, text_prompt: str) -> dict:
        """整体提交，和 `PUT /api/assets/{id}` 同一个约定：不留部分更新的口子。

        首次保存建行、之后改行，靠 `ON CONFLICT(asset_id) DO UPDATE`——
        老人物没有档案行，"先查再决定 INSERT 还是 UPDATE"要读两趟还有竞态。
        """
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO asset_character_profile
                       (asset_id, biography, personality, growth_arc, text_prompt)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(asset_id) DO UPDATE SET
                       biography = excluded.biography,
                       personality = excluded.personality,
                       growth_arc = excluded.growth_arc,
                       text_prompt = excluded.text_prompt,
                       updated_at = datetime('now','localtime')""",
                (asset_id, biography, personality, growth_arc, text_prompt))
            touch(conn, "asset", asset_id)
        return self.get(asset_id)


class VariantRepository:
    """命名形态：人物的服饰、场景的视角、道具的形态。

    图跟着形态走、不进 `asset.ref_images`，这是这张表存在的唯一理由：把"日常常服"
    和"受伤状态"混进同一个数组，下游 I2V 会拿互相矛盾的服装当同一组参考图去生——
    那比没有参考图更糟。基础参考图仍然留在 `asset.ref_images`，旧的生成流程照用。
    """

    TYPES = ("outfit", "scene_view", "prop_form", "other")

    def list_by_asset(self, asset_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM asset_variant WHERE asset_id = ? ORDER BY sort_order, id",
                (asset_id,)).fetchall()

    def get(self, variant_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                                (variant_id,)).fetchone()

    def exists_name(self, asset_id: int, variant_type: str, name: str,
                    exclude_id: int | None = None) -> bool:
        """同一资产、同一类型下查重。撞名要回人可以改，不自动加序号。"""
        sql = "SELECT 1 FROM asset_variant WHERE asset_id = ? AND variant_type = ? AND name = ?"
        params: list = [asset_id, variant_type, name]
        if exclude_id is not None:
            sql += " AND id <> ?"
            params.append(exclude_id)
        with get_conn() as conn:
            return conn.execute(sql, params).fetchone() is not None

    def create(self, *, asset_id: int, variant_type: str, name: str, prompt: str = "",
               source_job_id: int | None = None,
               sort_order: int | None = None) -> sqlite3.Row:
        """建一个形态。不给 sort_order 就排到最后——号在同一事务里数，
        两个人同时建不会拿到同一个号（`episode.seq` 同一条纪律）。"""
        with get_conn() as conn:
            if sort_order is None:
                sort_order = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM asset_variant "
                    "WHERE asset_id = ?", (asset_id,)).fetchone()["n"]
            cur = conn.execute(
                """INSERT INTO asset_variant
                       (asset_id, variant_type, name, prompt, source_job_id, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (asset_id, variant_type, name, prompt, source_job_id, sort_order))
            touch(conn, "asset", asset_id)
            return conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                                (cur.lastrowid,)).fetchone()

    def update(self, variant_id: int, *, variant_type: str, name: str, prompt: str,
               sort_order: int) -> sqlite3.Row | None:
        """整体改可编辑字段。**不接受 asset_id**：形态挂在哪个资产不是编辑项，
        改一次就等于把某人的衣服挂到某个场景上，那是搬运不是修改。"""
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM asset_variant WHERE id = ?",
                            (variant_id,)).fetchone() is None:
                return None
            conn.execute(
                """UPDATE asset_variant SET variant_type = ?, name = ?, prompt = ?,
                          sort_order = ?, updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (variant_type, name, prompt, sort_order, variant_id))
            owner = conn.execute("SELECT asset_id FROM asset_variant WHERE id = ?",
                                 (variant_id,)).fetchone()
            touch(conn, "asset", owner["asset_id"])
            return conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                                (variant_id,)).fetchone()

    def delete(self, variant_id: int) -> sqlite3.Row | None:
        """删记录，并把被删的那行还回去。

        调用方要拿它的路径去清磁盘上的图——行先没了就查不到路径，
        文件会变成没人认领的孤儿（`delete_project_cascade` 踩过一次）。
        """
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                               (variant_id,)).fetchone()
            if row is None:
                return None
            touch(conn, "asset", row["asset_id"])
            conn.execute("DELETE FROM asset_variant WHERE id = ?", (variant_id,))
            return row

    def add_image(self, variant_id: int, relative_path: str,
                  source_job_id: int | None = None) -> sqlite3.Row | None:
        """往形态的图清单里追加一张。

        读-改-写在同一个连接里；上层（导入产物的服务函数）套 `transaction()`
        时这里是 BEGIN IMMEDIATE，两个人同时往同一个形态存图不会互相覆盖那份 JSON。
        `source_job_id` 用 COALESCE：追加图时可以顺带补来源，也可以不动原来的来源。
        """
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                               (variant_id,)).fetchone()
            if row is None:
                return None
            images = _images_of(row)
            images.append(relative_path)
            conn.execute(
                """UPDATE asset_variant SET ref_images = ?,
                          source_job_id = COALESCE(?, source_job_id),
                          updated_at = datetime('now','localtime') WHERE id = ?""",
                (json.dumps(images, ensure_ascii=False), source_job_id, variant_id))
            touch(conn, "asset", row["asset_id"])
            return conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                                (variant_id,)).fetchone()

    def remove_image(self, variant_id: int,
                     relative_path: str) -> tuple[sqlite3.Row | None, bool]:
        """摘掉一张图，返回 (形态行, 是否确实删掉了)。路径不在清单里不算失败，
        但也不许假装删掉了——调用方据此决定要不要删磁盘上那个文件。"""
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                               (variant_id,)).fetchone()
            if row is None:
                return None, False
            images = _images_of(row)
            if relative_path not in images:
                return self.get(variant_id), False
            images.remove(relative_path)
            conn.execute(
                """UPDATE asset_variant SET ref_images = ?,
                          updated_at = datetime('now','localtime') WHERE id = ?""",
                (json.dumps(images, ensure_ascii=False), variant_id))
            touch(conn, "asset", row["asset_id"])
            return conn.execute("SELECT * FROM asset_variant WHERE id = ?",
                                (variant_id,)).fetchone(), True

    def get_many(self, variant_ids: list[int] | set[int]) -> dict[int, sqlite3.Row]:
        """一次读回一组形态（指纹与分镜标签都用它），键是 id。

        和 `assets.get_many` 同一条理由：一集几十镜逐个 `get()` 会把一次列表打成
        几十趟回库。查不到的 id 不出现在结果里——调用方要区分"形态被删了"。
        """
        ids = sorted({int(i) for i in variant_ids})
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM asset_variant WHERE id IN ({marks})", ids).fetchall()
        return {int(row["id"]): row for row in rows}

    def usable_ids(self, project_id: int, variant_ids: list[int]) -> set[int]:
        """这一组形态 id 里，哪些是该项目**可以引用**的：父资产是自有或全局。

        判的是父资产的归属，不是形态自己——形态没有独立的可见性（0033 的纪律）。
        和 `assets.usable_ids` 一样只回 id：镜头保存时只需要"这组引用成不成立"。
        """
        ids = [int(i) for i in variant_ids]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT v.id FROM asset_variant v
                     JOIN asset a ON a.id = v.asset_id
                     WHERE v.id IN ({marks})
                       AND (a.project_id = ? OR a.project_id IS NULL)""",
                [*ids, project_id],
            ).fetchall()
        return {row["id"] for row in rows}

    def parents_of(self, variant_ids: list[int]) -> dict[int, int]:
        """形态 → 父资产 id。镜头写入口用它把"引用了谁的形态"补进资产引用。"""
        ids = [int(i) for i in variant_ids]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"SELECT id, asset_id FROM asset_variant WHERE id IN ({marks})", ids).fetchall()
        return {row["id"]: row["asset_id"] for row in rows}

    def referencing_shots(self, asset_id: int) -> list[sqlite3.Row]:
        """哪些镜头把这个资产的**某个形态**指到了分镜上（0034）。

        与 `AssetRepository.referencing_shots` 同一写法：走 `json_each` 而不是 LIKE，
        键带 shot_id 回去给调用方拼进那份引用清单。这里只查本资产的形态
        （`v.asset_id = ?`），别的资产的形态不该出现在这条资产的引用清单里。
        """
        with get_conn() as conn:
            return conn.execute(
                """SELECT s.id AS shot_id, v.name AS variant_name
                     FROM shot s
                     JOIN json_each(s.variant_ids) j
                     JOIN asset_variant v ON v.id = CAST(j.value AS INTEGER)
                    WHERE json_valid(s.variant_ids) AND v.asset_id = ?
                    ORDER BY s.id""",
                (asset_id,),
            ).fetchall()

    def images_by_project(self, project_id: int) -> list[str]:
        """这个项目下所有形态的图路径。

        删项目时 `projects.asset_images()` 只看 `asset.ref_images`，不看这里
        就会把形态图漏成孤儿文件——所以那两个清单必须一起交给清理流程。
        """
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT v.ref_images FROM asset_variant v
                     JOIN asset a ON a.id = v.asset_id WHERE a.project_id = ?""",
                (project_id,)).fetchall()
        out: list[str] = []
        for row in rows:
            out.extend(_images_of(row))
        return out


episodes = EpisodeRepository()
shots = ShotRepository()
assets = AssetRepository()
profiles = CharacterProfileRepository()
variants = VariantRepository()
