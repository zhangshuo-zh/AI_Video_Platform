"""网页上设的本地运行时路径：存取那几行，并把"人设的"与"进程生效的"分开说清（0051）。

四条纪律，都与这张表存在的理由绑在一起：

1. **只存路径，绝不存密钥。** `adapters.py` 文件头第 3 条边界是"密钥只认变量名，
   值永远只从进程环境读"。migration 的 CHECK 白名单已经拦住了往
   `DEEPSEEK_API_KEY` 之类的行里写东西；这里再守一道 `KEYBOARD_ONLY`——
   两道各自独立，任何一道单独失效都不会让密钥进库。

2. **NULL ≠ 空串**。库里没值返回 None，语义是"没人在网页上设过"；设了空串
   等于清除覆盖，写入前归一成 NULL。两种"没设"不该有两种表示（同 0047 的判据）。

3. **环境变量永远优先。** 这一层只回答"网页上设了什么"，不回答"最终生效多少"。
   `config` / `runtime` 在解析时按「环境变量 > 本表 > 内置默认」三级回落，
   所以部署脚本、CI、`run.sh` 的推导不会被网页上的值悄悄盖掉。

4. **不在这里应用。** 把值塞进 `os.environ` 是调用方的事（`runtime_paths.apply()`），
   因为那一步有副作用、且必须考虑 worker 子进程要不要重启。存储层保持无副作用。
"""
from __future__ import annotations

import sqlite3

from db import get_conn

# 允许通过页面配置的变量：与 migration 0051 的 CHECK 白名单逐字一致。
# 两处都写是故意的——migration 管"库里的既有行"，这里管"新写入的请求"，
# 谁先失效都不会放行一个不该放行的名字。
#
# **这个元组里永远不该出现任何 `*_API_KEY` / `*_KEY` / `*_TOKEN`。**
# 它们归 `adapters.key_env`，只从进程环境读，连"值"这个概念都不该进库。
ALLOWED_ENV = (
    "FASTVIDEO_FASTMETAL_HOME",
    "FASTVIDEO_FASTMETAL_MODEL",
    "FASTVIDEO_MLX_VIDEO_HOME",
    "FASTVIDEO_MLX_VIDEO_MODEL",
    "FASTVIDEO_MFLUX_HOME",
    "FASTVIDEO_MFLUX_BIN",
    "FASTVIDEO_VOXCPM_HOME",
    "FASTVIDEO_ACE_STEP_HOME",
    "FASTVIDEO_MLX_AUDIO_BIN",
    "FASTVIDEO_RIFE_WEIGHTS",
)

SUBJECT = "runtime_path"


def _assert_allowed(env_name: str) -> None:
    """不在白名单就当场拒。绝不做"顺手也存一下"的宽容。"""
    if env_name not in ALLOWED_ENV:
        raise ValueError(f"{env_name} 不是允许在网页上配置的运行时路径变量")
    # 兜底：即使有人误把密钥变量名加进 ALLOWED_ENV，这里也再拦一次
    upper = env_name.upper()
    if any(marker in upper for marker in ("API_KEY", "TOKEN", "SECRET", "_KEY")):
        raise ValueError(f"{env_name} 看着是密钥类变量，不允许进库")


def get_all() -> dict[str, str]:
    """所有**在网页上设过**的变量 → 路径。没设过的不出现在返回值里。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT env_name, path FROM runtime_path WHERE path IS NOT NULL"
        ).fetchall()
    return {r["env_name"]: r["path"] for r in rows}


def get_one(env_name: str) -> str | None:
    """单个变量的覆盖值；没设过返回 None（不是空串）。"""
    _assert_allowed(env_name)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT path FROM runtime_path WHERE env_name = ?", (env_name,)
        ).fetchone()
    return (row["path"] if row else None) or None


def save(env_name: str, path: str | None, *,
         updated_by: int | None = None, note: str = "") -> str | None:
    """写一行（幂等 upsert）。`path` 为空串/None 表示清除覆盖。返回存下的值。"""
    _assert_allowed(env_name)
    cleaned = (path or "").strip() or None
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO runtime_path (env_name, path, updated_at, updated_by, note)
               VALUES (?, ?, datetime('now','localtime'), ?, ?)
               ON CONFLICT(env_name) DO UPDATE SET
                   path = excluded.path,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by,
                   note = excluded.note""",
            (env_name, cleaned, updated_by, note[:200]))
    return cleaned


def forget(env_name: str | None = None) -> None:
    """测试用：清掉一条或全部，回到"没人设过"。"""
    try:
        with get_conn() as conn:
            if env_name is None:
                conn.execute("DELETE FROM runtime_path")
            else:
                conn.execute("DELETE FROM runtime_path WHERE env_name = ?", (env_name,))
    except sqlite3.Error:
        pass
