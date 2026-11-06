"""引擎实例的读写（0040）。

这一层的职责只有一句话：**存配置、验配置，不判健康**。

为什么不存健康：0040 的表头写了三条边界，其中第二条是"存在性现查、不落库"
（沿用 0035 对 `artifact_path` 的那条注释）。探测结论一旦落列，就会出现
"库里写在线、`runtime.probe()` 说连不上"两份答案，而这两份永远会漂。
留历史的地方已经有了——`model_validation` 的 `env_check` 事件。

为什么在这里就验 endpoint 而不放到路由层：端点形状是**数据完整性**问题，
不是业务判断。校验只留一条路子是因为脚本、迁移工具、将来的第二个入口都得
受同一道约束；路由层再补的是"engine_code 在不在清单里"这种需要读清单的判断。

endpoint 的四条规矩，每条都对着一个真实事故形状：
  * 只收 `http://`——本机应用起的是明文 HTTP，收 `https://` 只会让人以为
    链路是加密的；也不收 `file:`／`unix:` 这类写法。
  * 不许带 `user:pass@`——那等于把凭据写进一个会进导出、日志与接口响应的字段。
  * 不许带查询串或片段——`?token=xxx` 是同一类泄漏路径。
  * 端口必须 1024—65535——与 Draw Things 自己的提示同口径，也是避开
    特权端口与"看起来像端口其实是路径"的写法。
"""
from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

from db import get_conn

# 连接方式有限枚举。加一档必须先有实现分支，不然界面会给出一个点了没反应的选项
# （CR-010：界面不许显示没有真实后端行为的按钮）。
CONNECTION_TYPES = ("local_http", "grpc", "cli")


class EngineInstanceError(ValueError):
    """实例层的业务拒绝。路由层统一转 422，把原话给人看。"""


def validate_endpoint(raw: str) -> str:
    """规范化并校验端点；不合法就抛，合法返回去掉尾斜杠的原文。"""
    text = (raw or "").strip()
    if not text:
        raise EngineInstanceError("endpoint 不能空——这一行的全部意义就是"
                                  "「这一档引擎在哪个地址」")
    parsed = urlparse(text)
    if parsed.scheme != "http":
        raise EngineInstanceError(f"endpoint 只接受 http:// 开头的地址，"
                                  f"现在是「{parsed.scheme or '没有 scheme'}」")
    if parsed.username or parsed.password:
        raise EngineInstanceError("endpoint 里不许带用户名或口令；"
                                  "凭据只走环境变量（shared_secret_env 那一格填变量名）")
    if parsed.query or parsed.fragment:
        raise EngineInstanceError("endpoint 不许带查询串或片段，只写 http://主机:端口")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise EngineInstanceError(f"endpoint 读不出主机与端口：{text}") from exc
    if not host:
        raise EngineInstanceError(f"endpoint 缺主机名：{text}")
    if port is None:
        raise EngineInstanceError("endpoint 必须显式写端口（例如 http://127.0.0.1:7860）")
    if not (1024 <= port <= 65535):
        raise EngineInstanceError(f"端口 {port} 不合法：只接受 1024—65535")
    if parsed.path not in ("", "/"):
        raise EngineInstanceError(f"endpoint 只写到主机与端口，不要带路径：{parsed.path}")
    return text.rstrip("/")


class EngineInstanceRepository:
    # ---------- 读 ----------

    def instances(self) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM engine_instance "
                "ORDER BY engine_code, endpoint"
            ).fetchall()

    def instance(self, instance_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM engine_instance WHERE id = ?", (instance_id,)
            ).fetchone()

    def pick(self, engine_code: str) -> sqlite3.Row | None:
        """这一档引擎当前该用哪个实例。

        只取 `enabled=1` 的第一条（按端点排序，结果稳定）。多条启用时不猜、
        也不轮询——W-B 第一阶段就是单实例；真要多实例时"选哪台"得变成作业上的
        显式选择，那是另一轮的事，绝不能在这里偷偷 round-robin。
        """
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM engine_instance WHERE engine_code = ? AND enabled = 1 "
                "ORDER BY endpoint LIMIT 1", (engine_code,)
            ).fetchone()

    # ---------- 写 ----------

    def create(self, data: dict) -> sqlite3.Row:
        connection_type = (data.get("connection_type") or "local_http").strip()
        if connection_type not in CONNECTION_TYPES:
            raise EngineInstanceError(
                f"连接方式只能是 {'、'.join(CONNECTION_TYPES)}，不是「{connection_type}」")
        endpoint = validate_endpoint(data.get("endpoint", ""))
        code = (data.get("engine_code") or "").strip()
        if not code:
            raise EngineInstanceError("engine_code 不能空")
        dup = self._find(code, endpoint)
        if dup is not None:
            raise EngineInstanceError(
                f"这一档引擎的这个端点已经登记过了（#{dup['id']}）；"
                "要改就编辑那一条，别再来一行——两行会让「现在用哪个」变成猜")
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO engine_instance
                        (engine_code, display_name, connection_type, endpoint,
                         shared_secret_env, enabled, note, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (code, (data.get("display_name") or "").strip(), connection_type,
                 endpoint, (data.get("shared_secret_env") or "").strip(),
                 1 if data.get("enabled", True) else 0,
                 (data.get("note") or "").strip(), data.get("created_by")),
            )
            return conn.execute("SELECT * FROM engine_instance WHERE id = ?",
                                (cursor.lastrowid,)).fetchone()

    def update(self, instance_id: int, patch: dict) -> sqlite3.Row:
        """逐字段更新。`engine_code` 与 `endpoint` 是这一行的键，改键＝换一台机器，
        那应该是新建一条——与 0035 对登记行「改键＝撤了再登记」同一口径。"""
        row = self.instance(instance_id)
        if row is None:
            raise EngineInstanceError(f"没有这条引擎实例：#{instance_id}")
        allowed = {"display_name", "connection_type", "shared_secret_env",
                   "enabled", "note"}
        unknown = set(patch) - allowed - {"endpoint"}
        if unknown:
            raise EngineInstanceError(f"这些字段不能改：{'、'.join(sorted(unknown))}")
        if patch.get("endpoint") not in (None, ""):
            raise EngineInstanceError("端点改不了：那一格连同 engine_code 是这一行的键。"
                                      "要换地址请新建一条，旧的停用，"
                                      "否则历史作业的「当时打的是哪台」就说不清了")
        fields: dict = {}
        if "connection_type" in patch:
            connection_type = (patch["connection_type"] or "").strip()
            if connection_type not in CONNECTION_TYPES:
                raise EngineInstanceError(
                    f"连接方式只能是 {'、'.join(CONNECTION_TYPES)}")
            fields["connection_type"] = connection_type
        for key in ("display_name", "shared_secret_env", "note"):
            if key in patch:
                fields[key] = str(patch[key] or "").strip()
        if "enabled" in patch:
            fields["enabled"] = 1 if patch["enabled"] else 0
        if not fields:
            return row
        sets = ", ".join(f"{key} = ?" for key in fields)
        with get_conn() as conn:
            conn.execute(
                f"UPDATE engine_instance SET {sets}, "
                f"updated_at = datetime('now','localtime') WHERE id = ?",
                (*fields.values(), instance_id))
            return conn.execute("SELECT * FROM engine_instance WHERE id = ?",
                                (instance_id,)).fetchone()

    def delete(self, instance_id: int) -> None:
        """撤实例。**不删任何历史作业**——作业行里冻结的是 provider/model 快照，
        不是这一行的外键，删掉它不会让旧作业变孤儿。"""
        with get_conn() as conn:
            conn.execute("DELETE FROM engine_instance WHERE id = ?", (instance_id,))

    def _find(self, engine_code: str, endpoint: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM engine_instance WHERE engine_code = ? AND endpoint = ?",
                (engine_code, endpoint)).fetchone()


engine_instances = EngineInstanceRepository()
