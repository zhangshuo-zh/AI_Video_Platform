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
               kind: str | None = None, model: str | None = None) -> sqlite3.Row | None:
        """某槽位最近一次验证（kind=None＝不分类型，取最后一条）。

        `model` 给了就按档取（0043）：记录里的 model 必须逐字相等——
        "1.3B 验过不等于 5B 验过"从此是 WHERE 的形状。不给保持旧口径
        （槽位内不分档），给还没升维的调用方留一条路。
        """
        with get_conn() as conn:
            if model is not None:
                conds = ["capability = ?", "backend = ?", "provider = ?", "model = ?"]
                args: list = [capability, backend, provider, model]
                if kind is not None:
                    conds.append("kind = ?")
                    args.append(kind)
                return conn.execute(
                    f"SELECT * FROM model_validation WHERE {' AND '.join(conds)}"
                    " ORDER BY id DESC LIMIT 1", tuple(args)).fetchone()
            if kind is None:
                return conn.execute(
                    """SELECT * FROM model_validation
                        WHERE capability = ? AND backend = ? AND provider = ?
                        ORDER BY id DESC LIMIT 1""",
                    (capability, backend, provider)).fetchone()
            return conn.execute(_LATEST_SQL, (capability, backend, provider, kind)).fetchone()

    def latest_by_kind(self, capability: str, backend: str,
                       provider: str, model: str | None = None) -> dict[str, sqlite3.Row]:
        """一次取回五类各自的最新记录。槽位健康那一列要同时用五档，逐类查会打五次。

        `model` 透传给 `latest()`：给了就按档取（0043）。能力轨那一列要回答
        "当前这一档能不能用"，所以 `summary_for` 会带上槽位当前的 model。
        """
        out: dict[str, sqlite3.Row] = {}
        for kind in ("env_check", "mock", "real_call", "target_machine", "artifact_review"):
            row = self.latest(capability, backend, provider, kind, model)
            if row is not None:
                out[kind] = row
        return out

    def latest_matrix(self, slots: set | None = None, *, by_model: bool = False
                      ) -> dict[tuple, dict[str, sqlite3.Row]]:
        """每个「能力×后端×供应商」槽位的五档最新验证。

        为什么要有这一条：`summary_for` 每个槽位要读五档，槽位有十个就是五十趟
        回库——而「模型与密钥」这一页一次就把十个槽位全渲染出来（连 `badge` 都要
        再读一次同一批行）。窗口函数一次算完，槽位数不再乘进查询次数。
        键是 (capability, backend, provider)，值是 {kind: 那一档最新的那条}。

        `slots` 是**定向查询**：给了就把 WHERE 收窄到这几个槽位，不给就全库算。
        为什么需要它：不带过滤的版本要把**全部历史**先过一遍窗口函数再丢弃 99%，
        而这张表只追加不改（表头的纪律一），跑一年之后"打开模型中心"每次都
        要扫全表。模型中心的目录只关心"已登记的那些槽位"，把它们传进来就只算那几组；
        设置页的十个槽位是固定的也能传。不给参数的调用（脚本、单点查）结果不变。

        `by_model`（0043）：分档取最新。默认 False 保持旧口径——同一槽位不分档、
        全混着取一条；True 时窗口按 (…, model, kind) 分组，键升成
        (capability, backend, provider, model)——"1.3B 验过不等于 5B 验过"
        从此是查询的形状，而不是每个读点各写一遍的补丁。给 `slots` 时 WHERE
        仍按三元组收窄（四元组的 model 不进 WHERE），分区在收窄后的行内
        按 model 切开：旧档的最新记录仍然查得到（历史不丢），只是不再替
        别的档说话。
        """
        partition = ("v.capability, v.backend, v.provider"
                     + (", v.model" if by_model else "")
                     + ", v.kind")
        sql = ("SELECT * FROM (SELECT v.*, ROW_NUMBER() OVER ("
               f" PARTITION BY {partition}"
               " ORDER BY v.id DESC) AS rn FROM model_validation v")
        args: list = []
        if slots:
            # 逐槽位展开成 `(cap=? AND backend=? AND provider=?) OR ...`：
            # SQLite 没有行值 IN，而槽位数最多几十个，拼出来的语句仍然很短。
            # 空集合走上面那条全库分支——"一个槽位都没有"意味着调用方还没读登记，
            # 那不是"查不到"，不该悄悄回空矩阵。
            sql += " WHERE " + " OR ".join(
                "(v.capability = ? AND v.backend = ? AND v.provider = ?)"
                for _ in slots)
            for entry in sorted(slots):
                args.extend(entry[:3])
        sql += ") WHERE rn = 1"
        with get_conn() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        out: dict[tuple, dict[str, sqlite3.Row]] = {}
        for row in rows:
            key = ((row["capability"], row["backend"], row["provider"], row["model"])
                   if by_model else
                   (row["capability"], row["backend"], row["provider"]))
            out.setdefault(key, {})[row["kind"]] = row
        return out

    # 一页最多回多少条。这不是"表里只有这么多"——它是**一次响应**的边界，
    # 所以调用方必须能翻页（见 `before_id`），不能像原来那样「return 前 200 条」就算完了：
    # 第 201 条之后的历史原来读不到，而"上周这台机器还连不连得上"恰恰要在历史里找。
    MAX_PAGE = 200

    def list_recent(self, limit: int = 50, before_id: int | None = None
                    ) -> list[sqlite3.Row]:
        """按 id 倒序一页。`before_id` 是上一页最后一条的 id（翻旧账用）。

        用 id 而不是 offset 翻页：model_validation 只追加不改（表头那三条纪律的第一条），
        所以"比某条更早"是个稳定边界；用 OFFSET 的话，翻页期间新写入一条就会让
        第二页整体挪一格，读到重复或漏行——而这张表恰恰一直在追加。
        """
        limit = max(1, min(int(limit), self.MAX_PAGE))
        sql = "SELECT * FROM model_validation"
        args: list = []
        if before_id is not None:
            sql += " WHERE id < ?"
            args.append(int(before_id))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with get_conn() as conn:
            return conn.execute(sql, tuple(args)).fetchall()

    def count(self) -> int:
        """历史总条数。给"一共多少条、这一页是第几页"用——
        只回一页而不说总量，人分不清"到底看完了没有"。"""
        with get_conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM model_validation")
                       .fetchone()[0])

    def for_job(self, job_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM model_validation WHERE job_id = ? ORDER BY id",
                (job_id,)).fetchall()


validations = ValidationRepository()
