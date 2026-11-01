"""模型验证记录的读写（P0）。

这张表只回答一件事：**这一槽位凭什么被认为能用**。

三条纪律：

1. **只增不改**。改了 provider 配置、换了权重、供应商换了模型，历史都得留在原地；
   否则"上次验证过"会变成一条被悄悄改写的结论。
2. **四类 provenance 不互串**。`env_check` 通过不等于 `real_call` 通过，
   `mock` 跑通更不等于真实供应商可用——这正是外部评审点名的那条。
3. **绝不存密钥值**。`checks_json` 里只允许出现变量名（`key_env_present: true`），
   值永远只在进程环境里。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

# 一个槽位"最新一次某类验证"的取法。列表页要按槽位聚合，不能每行各查一次。
_LATEST_SQL = """
    SELECT v.* FROM model_validation v
     WHERE v.id = (SELECT id FROM model_validation
                    WHERE capability = ? AND backend = ? AND provider = ?
                      AND kind = ?
                    ORDER BY id DESC LIMIT 1)
"""


class ValidationRepository:
    def record(self, data: dict) -> sqlite3.Row:
        """写一条验证记录。调用方负责把 provider/model/adapter 快照给全。"""
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO model_validation
                   (capability, backend, provider, model, adapter_code, manifest_version,
                    kind, state, detail, checks_json, params_json, job_id,
                    provider_request_id, http_status, latency_ms, artifact_path,
                    artifact_sha256, cost_micros, currency, paid_confirmed, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["capability"], data["backend"], data.get("provider", ""),
                 data.get("model", ""), data.get("adapter_code", ""),
                 data.get("manifest_version", ""), data["kind"], data["state"],
                 data.get("detail", "")[:2000],
                 json.dumps(data.get("checks", {}), ensure_ascii=False, sort_keys=True),
                 json.dumps(data.get("params", {}), ensure_ascii=False, sort_keys=True),
                 data.get("job_id"), data.get("provider_request_id"),
                 data.get("http_status"), data.get("latency_ms"),
                 data.get("artifact_path"), data.get("artifact_sha256"),
                 data.get("cost_micros"), data.get("currency"),
                 int(bool(data.get("paid_confirmed"))), data.get("created_by")),
            )
            return conn.execute("SELECT * FROM model_validation WHERE id = ?",
                                (cur.lastrowid,)).fetchone()

    def latest(self, capability: str, backend: str, provider: str,
               kind: str | None = None) -> sqlite3.Row | None:
        """某槽位最近一次验证（kind=None＝不分类型，取最后一条）。"""
        with get_conn() as conn:
            if kind is None:
                return conn.execute(
                    """SELECT * FROM model_validation
                        WHERE capability = ? AND backend = ? AND provider = ?
                        ORDER BY id DESC LIMIT 1""",
                    (capability, backend, provider)).fetchone()
            return conn.execute(_LATEST_SQL, (capability, backend, provider, kind)).fetchone()

    def latest_by_kind(self, capability: str, backend: str,
                       provider: str) -> dict[str, sqlite3.Row]:
        """一次取回四类各自的最新记录。槽位健康那一列要同时用四档，逐类查会打四次。"""
        out: dict[str, sqlite3.Row] = {}
        for kind in ("env_check", "mock", "real_call", "target_machine"):
            row = self.latest(capability, backend, provider, kind)
            if row is not None:
                out[kind] = row
        return out

    def list_recent(self, limit: int = 50) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM model_validation ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),)).fetchall()

    def for_job(self, job_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM model_validation WHERE job_id = ? ORDER BY id",
                (job_id,)).fetchall()


validations = ValidationRepository()
