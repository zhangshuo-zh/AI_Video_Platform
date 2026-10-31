"""模型双轨配置的读写。

密钥从不入库：这里只按 provider code 去环境变量里看一眼有没有配，
名字约定是 FV_KEY_<PROVIDER>，比如 provider 填 volc 就读 FV_KEY_VOLC。
"""
import os
import sqlite3

from db import get_conn


def key_env_name(provider: str) -> str:
    return "FV_KEY_" + provider.strip().upper().replace("-", "_")


def has_key(provider: str) -> bool:
    """provider 为空自然算没配；有值就看环境变量非空。"""
    if not provider.strip():
        return False
    return bool(os.environ.get(key_env_name(provider)))


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

    def replace_many(self, updates: list[dict]) -> None:
        """整批更新，一个事务里做完，要么全成要么全不动。"""
        with get_conn() as conn:
            for item in updates:
                conn.execute(
                    """UPDATE provider_config
                          SET provider = ?, model = ?, enabled = ?, is_default = ?,
                              updated_at = datetime('now','localtime')
                        WHERE id = ?""",
                    (item["provider"].strip(), item["model"].strip(),
                     int(item["enabled"]), int(item["is_default"]), item["id"]),
                )

providers = ProviderRepository()
