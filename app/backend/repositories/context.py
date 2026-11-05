"""当前项目 × 当前集：每人一行的服务端上下文（0037）。

为什么放在服务端而不是 localStorage：工作台、预检、审核、成片四个入口都要知道
"他现在站在哪一集"，而前端那份 `store.currentId` 只活一次会话、集根本没有服务端记录。
上下文一旦有两份，就会出现"顶栏写着第 3 集、工作台算的是第 1 集"。

这里只存"上次站在哪"，**不存权限**：能不能看这个项目仍由 `access.ensure` 判定，
写入前也要过一次可见性——否则任何人可以把别人的集塞进自己的上下文，
再顺着工作台读到别人的镜头计数。
"""
from __future__ import annotations

import sqlite3

from db import get_conn


class ContextRepository:
    def get(self, user_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM user_context WHERE user_id = ?", (user_id,)
            ).fetchone()

    def set(self, user_id: int, *, project_id: int | None,
            episode_id: int | None) -> sqlite3.Row:
        """整行覆盖。集为空表示"只认项目"（项目中心、资产中心就是这种状态）。

        episode_id 给定时由调用方保证它属于 project_id——这里不重复校验，
        因为校验要读 episode 行，而调用方（路由）本来就要读一次做可见性判断。
        """
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO user_context (user_id, project_id, episode_id, updated_at)
                   VALUES (?, ?, ?, datetime('now','localtime'))
                   ON CONFLICT(user_id) DO UPDATE
                     SET project_id = excluded.project_id,
                         episode_id = excluded.episode_id,
                         updated_at = datetime('now','localtime')""",
                (user_id, project_id, episode_id),
            )
            return conn.execute(
                "SELECT * FROM user_context WHERE user_id = ?", (user_id,)
            ).fetchone()


context = ContextRepository()
