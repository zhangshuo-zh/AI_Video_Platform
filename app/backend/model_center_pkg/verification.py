"""模型中心：verification 业务域。"""
from __future__ import annotations
from sqlite3 import Row



def verified_kinds(capability: str, backend: str, provider: str,
                   model: str = "") -> set[str]:
    """这一槽位**这一档**已经落过的验证出处（real_call / artifact_review / …）。

    用它决定人工声明能不能从"未核验"升级成"已验证"。

    0043 起按档取：验证记录的"档"升为索引维度（`idx_validation_slot_model`），
    `latest_matrix(by_model=True)` 直接按 (capability, backend, provider, model)
    给最新——"1.3B 验过不等于 5B 验过"从此是查询的形状。原来的读点补丁
    （调用方拿槽位矩阵后再比一次记录里的 model）随 0043 撤掉：
    分区键里就是 model，查询结果已经只含这一档的记录。

    `model` 给空串时匹配"记录里 model 也是空串"的历史行——那是早期
    env_check 没记档时留下的组，只与"槽位也没填 model"的配置互相对应。
    """
    from repositories.validation import validations as vrepo

    latest = vrepo.latest_matrix(by_model=True).get(
        (capability, backend, provider, model), {})
    return {kind for kind, row in latest.items() if row["state"] == "ok"}



def _verification_index(slots: set | None = None) -> dict[tuple, dict[str, Row]]:
    """一次查询拿"这些槽位×档各自的每类最新验证"，供 `catalog()` 复用。

    与 `verified_kinds()` 同一份数据，区别只是调用次数：一次目录构建里有几十行，
    每行再各查一次就是给"打开一页"买几十趟回库。矩阵一次读完后按键取，
    口径不变、查询数从 O(行数) 降到 1。

    `slots` 由调用方按"已登记的那几组能力×后端×供应商×模型档"给
    （`catalog()` 就是这么做的）：不给过滤条件时窗口函数要把**全部历史**
    先算一遍再丢掉绝大多数，而 `model_validation` 只追加不改，
    历史越长"打开模型中心"越慢。0043 起键是四元组——分档是查询的形状。
    """
    from repositories.validation import validations as vrepo

    return vrepo.latest_matrix(slots, by_model=True)



def _kinds_of(index: dict, capability: str, backend: str, provider: str,
              model: str) -> set[str]:
    latest = index.get((capability, backend, provider, model), {})
    return {kind for kind, row in latest.items() if row["state"] == "ok"}
