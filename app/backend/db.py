"""SQLite 连接管理。建表不在这里——那是 migrate.py 的事。

为什么用 contextmanager 而不是连接池：2—5 人、单进程、写操作低频，
一个请求一条连接最省心；WAL 让读不挡写。

`transaction()` 是这一轮补的例外：少数跨多表的写（删账号＝转移 N 个项目 ＋ 删一行）
必须是一个原子动作，不能让仓储层"一个方法一条连接一次提交"把它切成十几段。
"""
import contextvars
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

# 当前正在使用的事务连接。放在 contextvar 而不是函数参数上：
# 仓储层的签名不必为了事务改一遍，也不用把 conn 一路透传到每个方法。
# FastAPI 每个请求一个上下文、worker 单线程顺序跑，两边都不会串线。
_HELD = contextvars.ContextVar("fv_held_conn", default=None)


def _connect() -> sqlite3.Connection:
    # timeout=5：写冲突时最多等 5 秒再报 database is locked。
    # 多人同时提交作业时，这一条比任何配置都管用。
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # 这两条 PRAGMA 必须在 BEGIN 之前执行：journal_mode 在事务里改会直接报错
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn():
    held = _HELD.get()
    if held is not None:
        # 已经在 transaction() 里：这条连接由 transaction() 收尾，
        # 这里既不能 commit 也不能 close——提前提交就等于把半个事务写出去了
        yield held
        return
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction():
    """把一段跨表写收进**同一个**事务；里面的每次 `get_conn()` 借用同一条连接。

    为什么需要：仓储层每个方法各开一条连接、各提交一次，于是"删账号"实际上是
    十几个独立事务——第 3 个项目转移失败时前 2 个已经落库、账号还在，
    库里留下半套状态，而且没有任何人会立刻发现。
    `BEGIN IMMEDIATE` 与 `jobs.claim_next` 同一写法：写锁一开始就拿到，
    中途不会有别人的写插进来。

    一条边界：**事务里只做库内写**。要动 Git 提交或删文件的那些步骤不包进来——
    文件删了、提交做了，事务回滚不会把它们带回去，"看起来原子"比不原子更危险。
    那种流程改成"做完再收口，中途失败就把已经做到的部分讲清楚"。
    """
    held = _HELD.get()
    if held is not None:
        yield held          # 嵌套：沿用外层那一个事务，收尾由外层负责
        return
    conn = _connect()
    token = _HELD.set(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _HELD.reset(token)
        conn.close()
