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

import safenet
from db import get_conn

# 连接方式。**这里有两把，别混用**（2026-11-07 第三批分开）：
#
#   CONNECTION_TYPES        ＝ 0040 的 CHECK 认哪些值。**存储域**，只能扩不能缩，
#                              缩了会让库里已有的行读不出来。它回答"能存什么"。
#   SUPPORTED_CONNECTION_TYPES ＝ 今天**真的有实现分支**的那几档。它回答"能选什么"。
#
# 为什么要分开：原来只有一把，于是 `grpc`／`cli` 是"存得进、界面上也选得到、但探测
# 与执行都没有对应分支"的选项——`engines.probe()` 根本不看 connection_type，一律按
# HTTP 打端点。选 grpc 的人会看到一个 http 探测的结论，那不是他配的东西的结论。
# 这正是本文件开头那句 CR-010 要防的形状："界面不许给出一个点了没反应的选项"。
#
# 所以：接口只让选 SUPPORTED，拒绝时明说"这一档还没有实现分支，加它得先写探测与执行"，
# 而不是把人挡在一句"只能是 local_http"上就完事。库里若有历史行是别的值，
# 读路径照常列出来，但探测那一格会写"这一档连接方式还没有探测分支"，不假装探过。
CONNECTION_TYPES = ("local_http", "grpc", "cli")
SUPPORTED_CONNECTION_TYPES = ("local_http",)


class EngineInstanceError(ValueError):
    """实例层的业务拒绝。路由层统一转 422，把原话给人看。"""


def _check_connection_type(value: str) -> None:
    """写入前的那道闸：只收**今天真有实现分支**的连接方式。

    两种拒绝要分开说，因为人要做的事不同：
      * 这个值根本不在 0040 的 CHECK 里 → 拼错了，或者想加一档新形态；
      * 这个值 CHECK 收、但今天没有探测与执行分支 → 得先写代码，不是改配置。
    第二种如果混进第一种那句话里，人只会以为是自己填错，反复试。
    """
    if value in SUPPORTED_CONNECTION_TYPES:
        return
    if value in CONNECTION_TYPES:
        raise EngineInstanceError(
            f"连接方式「{value}」还没有实现分支：0040 的 CHECK 收这个值，但探测与执行"
            f"都只写了 HTTP 那一条路。今天能选的是 {'、'.join(SUPPORTED_CONNECTION_TYPES)}；"
            "要加一档得先给 engines.py 写探测、给执行器写分支，不是在这里改个枚举")
    raise EngineInstanceError(
        f"连接方式只能是 {'、'.join(SUPPORTED_CONNECTION_TYPES)}，不是「{value}」")


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
    # 第五条规矩（2026-11-08 补）：不许指向云元数据接口。
    # 本机与局域网是**正常用法**（注释里那三个例子就是 127.0.0.1/192.168.x），
    # 所以这里不封 loopback/私网，只拒"不可能是一台模型服务"的链路本地地址
    # 与 metadata 主机名——`engines.probe()` 会照着这个端点发 GET 并把失败原因
    # 回显给界面，那等于给了一个内网探测口（见 safenet 模块头）。
    try:
        safenet.check_url(text)
    except safenet.UnsafeTarget as exc:
        raise EngineInstanceError(f"endpoint 指向了平台不予放行的地址：{exc}") from exc
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
        _check_connection_type(connection_type)
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
            _check_connection_type(connection_type)
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


class EngineProbeLogRepository:
    """探测留痕（0042）。只追加不改，随实例 CASCADE。

    这一层只做两件事：写一条、读最近几条。**它不参与任何判断**——
    "现在是好的吗"永远由 `engines.probe()` 现查，历史上探通过不代表现在通。
    读它的人要看的是一句话："这台上周还连不连得上"。
    """

    def record(self, instance_id: int, status, *, source: str = "manual",
               latency_ms: int | None = None, created_by: int | None = None,
               engine_code: str = "", endpoint: str = "") -> None:
        """记一次探测。`status` 是 `engines.EngineStatus`（或同形状的 dict）。

        故意**不让这里抛**：留痕失败不该把一次成功的探测变成 500。
        人按「探测」要的是"现在通不通"，那是已经得到的结论；
        历史没记上是这一屏的次要信息，下次探测会补上。
        （真正的写库错误会由 `get_conn()` 的事务本身暴露在日志里。）
        """
        # `EngineStatus` 是 frozen dataclass（没有 __getitem__），调用方也可能传 dict
        # — 两种形状都收，但要用 hasattr 分派：写成 `getattr(...) or status["state"]`
        # 会在 dataclass 上先取到真值、再对 getattr 为空的 detail 去取下标——
        # 那不是等价回退，是 TypeError（第一版就这么写的，测试当场红）。
        def field(name, default):
            if hasattr(status, name):
                return getattr(status, name)
            return status.get(name, default)

        state = field("state", "unknown")
        detail = field("detail", "") or ""
        values = field("values", {}) or {}
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO engine_probe_log
                       (instance_id, engine_code, endpoint, state, engine_model,
                        detail, latency_ms, source, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (instance_id, engine_code, endpoint, state,
                 str(values.get("model") or ""), str(detail or "")[:2000],
                 latency_ms, source, created_by))

    def recent(self, instance_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """这条实例最近 N 次探测，最新在前。"""
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM engine_probe_log WHERE instance_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (instance_id, max(1, min(int(limit), 100)))).fetchall()

    def latest_by_instance(self) -> dict[int, sqlite3.Row]:
        """一次性取每条实例最近的那一条——列表页要的是"上次什么时候、结果如何"，

        逐行各查一次就是 N 趟回库，与 `latest_matrix()` 同一个理由。"""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM (
                        SELECT l.*, ROW_NUMBER() OVER (
                                 PARTITION BY l.instance_id ORDER BY l.id DESC) AS rn
                          FROM engine_probe_log l WHERE l.instance_id IS NOT NULL
                      ) WHERE rn = 1""").fetchall()
        return {row["instance_id"]: row for row in rows}



# 探测留痕是另一张表、另一族读法，所以给它自己的仓储对象——
# 塞进 EngineInstanceRepository 会让配置与历史共用一条写路径。
engine_probe_log = EngineProbeLogRepository()
