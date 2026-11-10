"""`secret_value` 表的读写（0052）。**密钥值只经由 `secret_store` 进出。**

这一层守两条：

1. **白名单 + 密钥特征双门**（与 `repositories/runtime_path.py` 同一套纪律，
   但方向相反：那边是"拦住密钥"，这边是"只放密钥"）。`assert_allowed` 要求
   名字既在 8 个已知供应商变量里，**又**带密钥特征——两个条件同时满足。
   为什么两道：只靠白名单时，将来有人往 `adapters.py` 加一个不是密钥的
   `key_env`（比如某个本地服务的端口变量）会顺手被允许；只靠特征时，
   `PLATFORM_SECRET_KEK` 这种名字本身带 SECRET 却绝不该入库的会被放进来。
   两道一夹，两边都挡住。
2. **不在这里解密、也不在这里记日志**。`get_row()` 给的是原始 BLOB，
   解密是 `secret_store.unseal` 的事；分开之后"谁会碰到明文"只有一处。

`save()` 收**明文**，但立刻交给 `secret_store.seal()` 转密文再落库——
调用方不需要也不应该自己加密（自己加密会漏掉 nonce/AAD 这两条的纪律）。
"""

from __future__ import annotations

import sqlite3

from db import get_conn

# 与 migrations/0052 的 CHECK 白名单**必须一致**。两处都写是刻意的冗余：
# 迁移管"库结构不许写错"，这里管"代码路径不许写错"，CI 有用例比对两处。
ALLOWED_ENV: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "FV_KEY_JIMENG",
    "FV_KEY_KLING",
    "FV_KEY_MINIMAX",
    "FV_KEY_SEEDANCE",
    "FV_KEY_VOLC",
    "FV_KEY_WANX",
)

# 密钥特征。**与 runtime_path 那边的拒绝逻辑方向相反**：那边见到这些就拒，
# 这边见到才放。仍然保留是因为白名单之外还需要一道"这名字看着像密钥吗"，
# 挡住"有人把非密钥的 key_env 加进 adapters 又被本表收下"。
_KEY_SHAPED = ("API_KEY", "_KEY", "_TOKEN", "SECRET", "PASSWORD")


def assert_allowed(env_name: str) -> None:
    """不在白名单、或看着不像密钥 → ValueError（**不是** sqlite 约束错）。"""
    if env_name not in ALLOWED_ENV:
        raise ValueError(
            f"{env_name!r} 不在密钥变量白名单里；"
            f"允许的是 {', '.join(ALLOWED_ENV)}")
    if not any(marker in env_name.upper() for marker in _KEY_SHAPED):
        raise ValueError(
            f"{env_name!r} 看着不像密钥变量（不含 {'/'.join(_KEY_SHAPED)} 任一）；"
            f"路径类配置请走 runtime_path 那张表")


def get_row(env_name: str) -> sqlite3.Row | None:
    """原始行（含 BLOB）。**不解密**。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM secret_value WHERE env_name = ?", (env_name,)
        ).fetchone()


def list_rows() -> list[sqlite3.Row]:
    """全部行（含 BLOB，供内部使用）。页面请用 `secret_store.peek()`。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM secret_value ORDER BY env_name").fetchall()


def save(env_name: str, plain: str, *, updated_by: int | None = None,
         note: str = "") -> dict:
    """写入（或覆盖）一把密钥。收**明文**，落库前转密文。

    返回 `{fingerprint, length}`——调用方拿它回显，不必也不该回显明文。
    """
    import secret_store
    assert_allowed(env_name)
    sealed = secret_store.seal(env_name, plain)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO secret_value
                   (env_name, ciphertext, nonce, aad, fingerprint, length,
                    updated_at, updated_by, note)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'), ?, ?)
               ON CONFLICT(env_name) DO UPDATE SET
                   ciphertext = excluded.ciphertext,
                   nonce      = excluded.nonce,
                   aad        = excluded.aad,
                   fingerprint= excluded.fingerprint,
                   length     = excluded.length,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by,
                   note       = excluded.note""",
            (env_name, sealed["ciphertext"], sealed["nonce"], sealed["aad"],
             sealed["fingerprint"], sealed["length"], updated_by, note))
    return {"fingerprint": sealed["fingerprint"], "length": sealed["length"]}


def forget(env_name: str) -> bool:
    """删掉库里的值（回落到"只认环境变量"）。返回是否真的删了一行。"""
    assert_allowed(env_name)
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM secret_value WHERE env_name = ?",
                           (env_name,))
    return cur.rowcount > 0
