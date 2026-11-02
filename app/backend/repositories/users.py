"""用户、角色、会话的读写。同样是"SQL 只在这一层"。

权限解析的规则：
- is_superuser = 1 → 拥有全部权限点，且以后新增的权限点自动生效；
- 否则 = 该用户所有角色的权限并集；
- 停用的账号（is_active=0）一律返回空集合。
"""
import sqlite3

from db import get_conn


class UserRepository:
    def get_by_username(self, username: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM app_user WHERE username = ?", (username,)
            ).fetchone()

    def get_by_id(self, user_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM app_user WHERE id = ?", (user_id,)
            ).fetchone()

    def count(self) -> int:
        with get_conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM app_user").fetchone()["n"]

    def create(self, username: str, display_name: str, password_hash: str,
               *, is_superuser: bool = False, must_change_password: bool = False,
               role_codes: list[str] | None = None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO app_user
                   (username, display_name, password_hash, is_superuser, must_change_password)
                   VALUES (?, ?, ?, ?, ?)""",
                (username, display_name, password_hash,
                 int(is_superuser), int(must_change_password)),
            )
            user_id = cursor.lastrowid
            for code in role_codes or []:
                conn.execute(
                    """INSERT OR IGNORE INTO user_role (user_id, role_id)
                       SELECT ?, id FROM role WHERE code = ?""",
                    (user_id, code),
                )
            return conn.execute(
                "SELECT * FROM app_user WHERE id = ?", (user_id,)
            ).fetchone()

    def touch_login(self, user_id: int) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE app_user SET last_login_at = datetime('now','localtime') WHERE id = ?",
                (user_id,),
            )

    def update_password(self, user_id: int, password_hash: str) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE app_user
                      SET password_hash = ?, must_change_password = 0
                    WHERE id = ?""",
                (password_hash, user_id),
            )

    def permissions_for(self, user: sqlite3.Row) -> set[str]:
        if not user["is_active"]:
            return set()
        with get_conn() as conn:
            if user["is_superuser"]:
                rows = conn.execute("SELECT code FROM permission").fetchall()
            else:
                rows = conn.execute(
                    """SELECT DISTINCT p.code
                         FROM permission p
                         JOIN role_permission rp ON rp.permission_id = p.id
                         JOIN user_role ur      ON ur.role_id = rp.role_id
                        WHERE ur.user_id = ?""",
                    (user["id"],),
                ).fetchall()
        return {row["code"] for row in rows}

    def roles_for(self, user_id: int) -> list[str]:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT r.code FROM role r
                     JOIN user_role ur ON ur.role_id = r.id
                    WHERE ur.user_id = ? ORDER BY r.code""",
                (user_id,),
            ).fetchall()
        return [row["code"] for row in rows]

    # ---------- 用户与角色管理（系统设置页用） ----------

    # 角色在一条 SQL 里带出来，免得用户列表 N+1
    _USER_SELECT = """
        SELECT u.id, u.username, u.display_name, u.is_superuser, u.is_active,
               u.must_change_password, u.created_at, u.last_login_at,
               COALESCE((SELECT json_group_array(r.code) FROM user_role ur
                           JOIN role r ON r.id = ur.role_id
                          WHERE ur.user_id = u.id), '[]') AS roles
          FROM app_user u
    """

    def list_all(self) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(self._USER_SELECT + " ORDER BY u.id").fetchall()

    def user_row(self, user_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(self._USER_SELECT + " WHERE u.id = ?", (user_id,)).fetchone()

    def set_active(self, user_id: int, active: bool) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE app_user SET is_active = ? WHERE id = ?",
                         (int(active), user_id))
            if not active:
                # 停用必须顺手踢掉在线会话，否则他手里那个浏览器还能接着用
                conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))

    def set_display_name(self, user_id: int, display_name: str) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE app_user SET display_name = ? WHERE id = ?",
                         (display_name, user_id))

    def set_roles(self, user_id: int, role_codes: list[str]) -> None:
        with get_conn() as conn:
            conn.execute("DELETE FROM user_role WHERE user_id = ?", (user_id,))
            for code in role_codes:
                conn.execute(
                    """INSERT OR IGNORE INTO user_role (user_id, role_id)
                       SELECT ?, id FROM role WHERE code = ?""",
                    (user_id, code),
                )

    def reset_password(self, user_id: int, password_hash: str) -> None:
        """管理员重置：标成"下次登录必须改"，并把现有会话全部作废。"""
        with get_conn() as conn:
            conn.execute(
                """UPDATE app_user SET password_hash = ?, must_change_password = 1
                    WHERE id = ?""",
                (password_hash, user_id),
            )
            conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))

    def delete(self, user_id: int) -> None:
        """删账号本身。会话与角色关系靠外键 CASCADE 跟着走。

        名下的项目不在这里处理：project.owner_id 是 ON DELETE RESTRICT，
        漏了转移或连带删这一步会直接报错，而不是悄悄留下悬空项目。
        """
        with get_conn() as conn:
            conn.execute("DELETE FROM app_user WHERE id = ?", (user_id,))

    def all_roles(self) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT r.id, r.code, r.name, r.description,
                          COALESCE((SELECT json_group_array(p.code)
                                      FROM role_permission rp
                                      JOIN permission p ON p.id = rp.permission_id
                                     WHERE rp.role_id = r.id), '[]') AS permissions
                     FROM role r ORDER BY r.id""",
            ).fetchall()

    def all_permissions(self) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT code, name, group_name FROM permission ORDER BY id"
            ).fetchall()

    def set_role_permissions(self, role_id: int, codes: list[str]) -> bool:
        """整组替换。返回 False 表示角色不存在。"""
        with get_conn() as conn:
            if conn.execute("SELECT 1 FROM role WHERE id = ?", (role_id,)).fetchone() is None:
                return False
            conn.execute("DELETE FROM role_permission WHERE role_id = ?", (role_id,))
            for code in codes:
                conn.execute(
                    """INSERT OR IGNORE INTO role_permission (role_id, permission_id)
                       SELECT ?, id FROM permission WHERE code = ?""",
                    (role_id, code),
                )
            return True


class SessionRepository:
    """会话落库：改密码或踢人时删一行就全网下线。"""

    # 会话行要带出去的列，一次列清楚。写 `u.*` 的时候 `password_hash` 会跟着一起
    # 变成"当前用户"对象——它本来不该出现在任何响应或缓存里，但只要列在那儿，
    # 下一步就有人顺手把它 print 进日志、塞进导出。少给一列才是保证，
    # `change_password` 那种真的要比对口令的地方改走 `users.get_by_id`。
    _USER_COLUMNS = ("u.id, u.username, u.display_name, u.is_active, "
                     "u.is_superuser, u.must_change_password")

    def create(self, token_hash: str, user_id: int, expires_at: str) -> None:
        with get_conn() as conn:
            # 顺手清掉过期行：会话是"到期即废"的，但废行不会自己消失——
            # 12 小时一期、一天十几次登录，几年下来库里攒的全是取不出来的死行。
            # 挂在这里而不是单独定时器，是因为这一层没有常驻任务，而登录
            # 恰好是唯一一个"每次都用到 session 表"的时刻。和插入同一条连接、
            # 同一个事务，不会把刚写进去的这行删掉（它比 now 晚）。
            conn.execute(
                "DELETE FROM session WHERE expires_at <= datetime('now','localtime')")
            conn.execute(
                "INSERT INTO session (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (token_hash, user_id, expires_at),
            )

    def get(self, token_hash: str) -> sqlite3.Row | None:
        # 返回当前用户的身份列（见 _USER_COLUMNS），这样调用方拿到的就是"这个人"，
        # 不用再查一次；口令哈希不在其中。
        with get_conn() as conn:
            return conn.execute(
                f"""SELECT {self._USER_COLUMNS}, s.expires_at
                      FROM session s JOIN app_user u ON u.id = s.user_id
                     WHERE s.token_hash = ?
                       AND s.expires_at > datetime('now','localtime')""",
                (token_hash,),
            ).fetchone()

    def delete(self, token_hash: str) -> None:
        with get_conn() as conn:
            conn.execute("DELETE FROM session WHERE token_hash = ?", (token_hash,))

    def delete_all_for_user(self, user_id: int) -> None:
        with get_conn() as conn:
            conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))

    def count(self) -> int:
        """会话行数。只给回归测试和备份核对用，不进业务返回。"""
        with get_conn() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]


users = UserRepository()
sessions = SessionRepository()
