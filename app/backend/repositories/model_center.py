"""模型登记与任务调用策略的读写（0035）。

两条边界，都是这张表能不能不变成"第二套清单"的关键：

1. **登记只存实例事实，不存规格副本**。参数范围、计价、出处一律回清单查
   （`adapters.py`），这里只存"这台机器上的这一档在不在、叫什么、开不开"。
   所以 `provider`/`model_code` 是**指向清单的键**，清单里没有也允许登记一行——
   但界面必须显示"清单未登记，参数不猜"，提交侧照样拒。
2. **删除只撤登记**。`delete()` 删的是这一行登记，不碰权重文件、不碰 `job`、
   不碰 `model_validation`。历史任务引用的是 job 里冻结的 provider/model 快照，
   撤一行登记不会让任何一条旧作业变成孤儿。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn
from repositories.ordering import backend_order, capability_order

# 顺序和 provider_config 那条一样按能力排，界面不用二次排序。
# CASE 片段只在 repositories/ordering.py 里有一份，这里拼一次给两处用。
_ORDER = f"{capability_order()}, {backend_order()}"


class RegistrationError(ValueError):
    """登记层的业务拒绝。路由层统一转 422，把原话给人看。"""


class ModelCenterRepository:
    # ---------- 登记 ----------

    def registrations(self) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                f"SELECT * FROM model_registration ORDER BY {_ORDER}, provider, model_code"
            ).fetchall()

    def registration(self, reg_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM model_registration WHERE id = ?", (reg_id,)
            ).fetchone()

    def find_registration(self, capability: str, backend: str,
                          provider: str, model_code: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM model_registration
                    WHERE capability = ? AND backend = ? AND provider = ? AND model_code = ?""",
                (capability, backend, provider, model_code),
            ).fetchone()

    def create(self, data: dict) -> sqlite3.Row:
        """新增登记。同键已存在就拒——静默覆盖会把人刚改的显示名与备注一起抹掉。"""
        dup = self.find_registration(data["capability"], data["backend"],
                                     data["provider"], data["model_code"])
        if dup is not None:
            raise RegistrationError(
                f"这一档已经登记过了（#{dup['id']}）：{data['capability']}"
                f"/{data['backend']}/{data['provider']}/{data['model_code']}。"
                "要改就编辑那一条；要登记同一档的另一个本地版本，改「版本」那一栏，"
                "不要再加一行——两行会让「这一档现在用哪个」变成猜。")
        caps = json.dumps(data.get("capabilities") or [], ensure_ascii=False)
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO model_registration
                   (capability, backend, provider, model_code, display_name, source,
                    engine_code, version, artifact_path, remote_model_id,
                    capabilities_json, enabled, note, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["capability"], data["backend"], data["provider"].strip(),
                 data["model_code"].strip(), data.get("display_name", "").strip(),
                 data.get("source", "local"), data.get("engine_code", "").strip(),
                 data.get("version", "").strip(), data.get("artifact_path", "").strip(),
                 data.get("remote_model_id", "").strip(), caps,
                 int(data.get("enabled", True)), data.get("note", "").strip(),
                 data.get("created_by")),
            )
            row = conn.execute(
                "SELECT * FROM model_registration ORDER BY id DESC LIMIT 1").fetchone()
        return row

    def update(self, reg_id: int, patch: dict) -> sqlite3.Row | None:
        """逐字段更新：只动 patch 里出现的键。

        不做整批覆盖（那是 provider_config 的做法）：登记行是逐条建的，
        一次保存全部会把别人刚加的一行连带写回旧值。
        """
        allowed = {"display_name", "source", "engine_code", "version", "artifact_path",
                   "remote_model_id", "enabled", "note", "capabilities"}
        unknown = [k for k in patch if k not in allowed]
        if unknown:
            raise RegistrationError(f"这些字段不能改：{unknown}")
        if not patch:
            return self.registration(reg_id)
        sets, vals = [], []
        for key in ("display_name", "source", "engine_code", "version",
                    "artifact_path", "remote_model_id", "note"):
            if key in patch:
                sets.append(f"{key} = ?")
                vals.append((patch[key] or "").strip())
        if "capabilities" in patch:
            sets.append("capabilities_json = ?")
            vals.append(json.dumps(patch["capabilities"] or [], ensure_ascii=False))
        if "enabled" in patch:
            sets.append("enabled = ?")
            vals.append(int(bool(patch["enabled"])))
        sets.append("updated_at = datetime('now','localtime')")
        vals.append(reg_id)
        with get_conn() as conn:
            conn.execute(
                f"UPDATE model_registration SET {', '.join(sets)} WHERE id = ?", vals)
        return self.registration(reg_id)

    def delete(self, reg_id: int) -> bool:
        """只撤登记行。权重文件与历史作业一律不碰。"""
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM model_registration WHERE id = ?", (reg_id,))
        return cur.rowcount > 0

    # ---------- 策略 ----------

    def policies(self) -> list[sqlite3.Row]:
        with get_conn() as conn:
            # 策略表没有 backend 列，所以只按能力排（同一个出处的那半边）
            return conn.execute(
                f"SELECT * FROM model_policy ORDER BY {capability_order()}, id"
            ).fetchall()

    def save_policies(self, items: list[dict]) -> int:
        """整批保存，一个事务：策略之间要互相看（同一能力两个场景抢同一默认轨不报错，
        但半批成功半批失败会让人以为配置生效了），所以要么全写要么全不写。"""
        n = 0
        with get_conn() as conn:
            for item in items:
                conn.execute(
                    """UPDATE model_policy
                          SET scene_label = CASE WHEN ? = '' THEN scene_label ELSE ? END,
                              capability = ?, required_json = ?,
                              slot_provider = ?, slot_model = ?, preference = ?,
                              fallback_provider = ?, fallback_model = ?,
                              resource_limit_json = ?, cloud_confirm_required = ?,
                              enabled = ?,
                              updated_by = ?,
                              updated_at = datetime('now','localtime')
                        WHERE scene_key = ?""",
                    # scene_label 空串＝"这次没打算改名"。场景名是 0035 种子给的，
                    # 整批保存时前端只回传配置项；照原样写会把 0035 里的中文名抹平，
                    # 于是"场景「」没有可用候选"这种话就出来了。
                    (item.get("scene_label", ""), item.get("scene_label", ""),
                     item["capability"],
                     json.dumps(item.get("required") or [], ensure_ascii=False),
                     item.get("slot_provider", "").strip(), item.get("slot_model", "").strip(),
                     item.get("preference", "any"),
                     item.get("fallback_provider", "").strip(),
                     item.get("fallback_model", "").strip(),
                     json.dumps(item.get("resource_limit") or {}, ensure_ascii=False),
                     int(bool(item.get("cloud_confirm_required"))),
                     int(bool(item.get("enabled"))), item.get("updated_by"),
                     item["scene_key"]),
                )
                n += conn.execute("SELECT changes()").fetchone()[0]
        return n


model_center = ModelCenterRepository()
