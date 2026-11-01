"""模型双轨配置的读写。

密钥从不入库：这里只按 provider 去环境变量里看一眼有没有配。变量名分三档：
用户在这一行填的 key_env > adapter 清单给的建议名（比如 deepseek 用
DEEPSEEK_API_KEY）> `FV_KEY_<PROVIDER>`。填的是**变量名**，不是值。
"""
import sqlite3

import adapters
from db import get_conn


def key_env_name(provider: str) -> str:
    """兜底约定：provider 填 volc 就读 FV_KEY_VOLC。"""
    return "FV_KEY_" + provider.strip().upper().replace("-", "_")


def has_key(row, env_name: str = "") -> bool:
    """row 是 provider_config 的一行；env_name 由调用方先算好（有效变量名）。

    留空时按老口径现算，给还没升级的调用方留一条路；新代码一律传 env_name，
    因为清单建议名（DEEPSEEK_API_KEY）只有 adapters 知道。
    """
    if row["backend"] != "cloud":
        # 本地运行时不用密钥；不要因为环境里恰好有个同名变量就显示"已配置"
        return False
    if not env_name:
        env_name = adapters.effective_key_env(
            row["provider"], row["key_env"] if "key_env" in row.keys() else "",
            row["capability"], row["backend"])
    return adapters.key_present(env_name)


class ProviderRepository:
    def all(self) -> list[sqlite3.Row]:
        with get_conn() as conn:
            # 顺序固定成 能力 → 本地在前云端在后，前端不用再排
            return conn.execute(
                """SELECT * FROM provider_config
                    ORDER BY CASE capability
                               WHEN 'text' THEN 1 WHEN 'image' THEN 2 WHEN 'video' THEN 3
                               WHEN 'tts'  THEN 4 ELSE 5 END,
                             CASE backend WHEN 'local' THEN 1 ELSE 2 END"""
            ).fetchall()

    def defaults(self) -> list[sqlite3.Row]:
        """每类能力打勾的那一条。没有打勾的就不会出现在结果里。

        解析路由时不能"缺一条就随便补一条"——那等于静默换轨。
        """
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM provider_config WHERE is_default = 1
                    ORDER BY CASE capability
                               WHEN 'text' THEN 1 WHEN 'image' THEN 2 WHEN 'video' THEN 3
                               WHEN 'tts'  THEN 4 ELSE 5 END"""
            ).fetchall()

    def slot(self, capability: str, backend: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM provider_config WHERE capability = ? AND backend = ?",
                (capability, backend),
            ).fetchone()

    def replace_many(self, updates: list[dict]) -> None:
        """整批更新，一个事务里做完，要么全成要么全不动。"""
        with get_conn() as conn:
            for item in updates:
                conn.execute(
                    """UPDATE provider_config
                          SET provider = ?, model = ?, enabled = ?, is_default = ?,
                              key_env = ?, notes = ?,
                              updated_at = datetime('now','localtime')
                        WHERE id = ?""",
                    (item["provider"].strip(), item["model"].strip(),
                     int(item["enabled"]), int(item["is_default"]),
                     (item.get("key_env") or "").strip(),
                     (item.get("notes") or "").strip(), item["id"]),
                )

providers = ProviderRepository()
