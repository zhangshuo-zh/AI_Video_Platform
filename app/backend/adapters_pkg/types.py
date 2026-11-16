"""适配器规格。修改规格时在对应能力模块维护。"""
from __future__ import annotations
from dataclasses import dataclass



@dataclass(frozen=True)
class Param:
    """一个允许下发的参数。范围/枚举写清楚，W07 生成命令时按这张表校验。"""

    name: str
    type: str                      # int / float / bool / str / image
    default: object | None = None
    minimum: float | None = None
    maximum: float | None = None
    multiple_of: int | None = None
    choices: tuple[str, ...] = ()  # 字符串枚举；空＝自由文本
    note: str = ""
    required: bool = True

    def as_dict(self) -> dict:
        out = {"name": self.name, "type": self.type, "default": self.default}
        if self.minimum is not None:
            out["min"] = self.minimum
        if self.maximum is not None:
            out["max"] = self.maximum
        if self.multiple_of is not None:
            out["multiple_of"] = self.multiple_of
        if self.choices:
            out["choices"] = list(self.choices)
        if self.note:
            out["note"] = self.note
        if not self.required:
            out["required"] = False
        return out



@dataclass(frozen=True)
class Price:
    """一条官方计价口径。只写核过的数字，不写"大概"。

    金额同样是整数 micros，且**按计价单位**存：`unit=second` 时是每秒多少
    micros，`unit=million_token` 时是每百万 token 多少 micros。界面拿它预填
    "数量 × 单价"，服务端仍然按提交人确认的那一张单子记账。
    """

    label: str
    unit: str
    currency: str
    amount_micros: int
    source: str
    default_quantity: float = 1.0
    when: tuple[tuple[str, object], ...] = ()   # 与 params 的键值同时成立才适用
    note: str = ""

    def as_dict(self) -> dict:
        out = {
            "label": self.label,
            "unit": self.unit,
            "currency": self.currency,
            "amount_micros": self.amount_micros,
            "source": self.source,
            "default_quantity": self.default_quantity,
            "note": self.note,
        }
        if self.when:
            out["when"] = {k: v for k, v in self.when}
        return out



@dataclass(frozen=True)
class ModelSpec:
    code: str
    label: str
    params: tuple[Param, ...] = ()
    prices: tuple[Price, ...] = ()
    note: str = ""
    # 这一档**确实**支持按 schema 出结构化结果（JSON schema / response_format）。
    # 为什么要一个显式字段而不是按能力轴推：`structured output` 是运行时行为，
    # 不是能力分类。今天只有本机 Ollama 那条腿有实证——`llm_http_run.py` 会把
    # `format` 原样发下去，且实测 `think=false` 时 1.9 秒回合法 JSON；
    # 云端 `cloud_run.py` 至今没有 response_format 分支，所以云端那几档一律不勾。
    # 勾了就等于对提交路径承诺"这一档能出 JSON"，而那件事要在执行器里真的成立。
    structured_output: bool = False

    def as_dict(self) -> dict:
        return {"code": self.code, "label": self.label,
                "params": [p.as_dict() for p in self.params],
                "prices": [p.as_dict() for p in self.prices],
                # 这一档能吃什么类型的**输入**，只从已登记的参数表里数出来
                # （type=="image" 就是它确实收一张图），不按模型名字猜：
                # "Wan2.2 I2V" 这个名字不足以说明 mflux 那一档也能收参考图——它不能。
                # 资产中心的图像面板据此老实说"这一档不接收参考图，一致性靠提示词文字"。
                "image_inputs": [p.name for p in self.params if p.type == "image"],
                "structured_output": bool(self.structured_output),
                "note": self.note}



@dataclass(frozen=True)
class Adapter:
    code: str
    label: str
    capability: str
    backend: str
    source: str
    models: tuple[ModelSpec, ...] = ()
    aliases: tuple[str, ...] = ()
    key_env: str = ""              # 云端建议的密钥变量名；本地留空
    home_env: str = ""             # 本地运行时目录的环境变量名
    params_source: str = ""        # 参数从哪读来的；空＝未核验
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "capability": self.capability,
            "backend": self.backend,
            "source": self.source,
            "models": [m.as_dict() for m in self.models],
            "aliases": list(self.aliases),
            "key_env": self.key_env,
            "home_env": self.home_env,
            "params_source": self.params_source,
            "params_known": bool(self.params_source),
            "note": self.note,
        }
