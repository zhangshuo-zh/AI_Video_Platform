"""用户、角色、会话的读写。同样是"SQL 只在这一层"。

权限解析的规则：
- is_superuser = 1 → 拥有全部权限点，且以后新增的权限点自动生效；
- 否则 = 该用户所有角色的权限并集；
- 停用的账号（is_active=0）一律返回空集合。
"""
import sqlite3
import threading
import time

from config import LOGIN_LOCK_SECONDS, LOGIN_MAX_FAILS
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
        #
        # `AND u.is_active = 1`（2026-11-08，P1）：停用账号必须立刻失效。
        # `set_active()` 停用时会顺手删会话行，那条路是好的；但只要有任何一条
        # 路径**只改 is_active 而没删会话**（迁移脚本、手工 SQL、将来新增的
        # 批量停用接口），旧会话就会继续有效——而 `require_user` 只看"查不查得到
        # 这个人"，于是 `/files/...`、`/api/context` 这些只挂 `require_user`
        # 的端点全部放行。权限空集只挡住了挂 `require_permission` 的那部分。
        # 把条件放在这条 SQL 里，等于把"停用即失效"绑在**唯一**的会话读取口上，
        # 不管是谁改的那一格，下一次请求就失效。
        with get_conn() as conn:
            return conn.execute(
                f"""SELECT {self._USER_COLUMNS}, s.expires_at
                      FROM session s JOIN app_user u ON u.id = s.user_id
                     WHERE s.token_hash = ?
                       AND s.expires_at > datetime('now','localtime')
                       AND u.is_active = 1""",
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


class LoginThrottle:
    """登录失败限速：同一「用户名+来源 IP」连续失败 N 次后锁一段时间。

    为什么不落库：这是**易于重建的瞬时状态**，不是业务事实。落表要迁移、
    要清理、要在备份里跟着走，而它全部的信息量就是"最近几分钟失败了几次"。
    进程重启后计数清零完全可以接受（局域网 2—5 人的部署，重启本身就很罕见）。

    为什么按「用户名+IP」而不是只按 IP：一个办公室常共用出口 IP，
    只按 IP 会让一个人输错三次、整个办公室一起被锁；只按用户名则挡不住
    "换着用户名撞"的扫描。两个都算上，既能拦住针对某个账号的暴力破解，
    又不至于让同事互相牵连。

    边界说清楚：进程内的字典**只覆盖本进程**。本平台是单进程 uvicorn
    （`run.sh` 起一条 API + 一条 worker，worker 不接 HTTP），所以够用；
    哪天上了多进程/多副本，这里要换成共享存储（Redis 或落库），
    否则每个进程各算一份，实际可试次数会乘上进程数。
    """

    def __init__(self, max_fails: int, lock_seconds: int):
        self.max_fails = max(0, int(max_fails))
        self.lock_seconds = max(0, int(lock_seconds))
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._locked: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str, client: str) -> tuple[str, str]:
        return ((username or "").strip().lower(), (client or "").strip())

    def _prune(self, key: tuple[str, str], now: float) -> None:
        """只留最近 `lock_seconds` 内的失败时刻——更早的失败不该继续累计。"""
        window = max(self.lock_seconds, 60)
        hits = [t for t in self._fails.get(key, []) if now - t < window]
        if hits:
            self._fails[key] = hits
        else:
            self._fails.pop(key, None)

    def retry_after(self, username: str, client: str) -> int:
        """还要等多少秒才能再试；0＝现在就可以试。"""
        if self.max_fails <= 0 or self.lock_seconds <= 0:
            return 0
        key = self._key(username, client)
        now = time.monotonic()
        with self._lock:
            until = self._locked.get(key)
            if until is not None:
                if until > now:
                    return max(1, int(round(until - now)))
                self._locked.pop(key, None)
                self._fails.pop(key, None)
            return 0

    def record_failure(self, username: str, client: str) -> None:
        if self.max_fails <= 0 or self.lock_seconds <= 0:
            return
        key = self._key(username, client)
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            hits = self._fails.setdefault(key, [])
            hits.append(now)
            if len(hits) >= self.max_fails:
                self._locked[key] = now + self.lock_seconds
                self._fails.pop(key, None)

    def reset(self, username: str, client: str) -> None:
        """登录成功：把这个组合的失败记录清掉。"""
        key = self._key(username, client)
        with self._lock:
            self._fails.pop(key, None)
            self._locked.pop(key, None)

    def clear(self) -> None:
        """全清。只给测试用。"""
        with self._lock:
            self._fails.clear()
            self._locked.clear()


login_throttle = LoginThrottle(LOGIN_MAX_FAILS, LOGIN_LOCK_SECONDS)
