"""SQLite 连接管理。建表不在这里——那是 migrate.py 的事。

为什么用 contextmanager 而不是连接池：2—5 人、单进程、写操作低频，
一个请求一条连接最省心；WAL 让读不挡写。
"""
import sqlite3
from contextlib import contextmanager

from config import DB_PATH


@contextmanager
def get_conn():
    # timeout=5：写冲突时最多等 5 秒再报 database is locked。
    # 多人同时提交作业时，这一条比任何配置都管用。
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
