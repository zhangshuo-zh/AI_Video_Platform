"""给"能力／运行位置"这两列排序的 SQL 片段——唯一出处。

为什么要单独一个文件：这一段 `CASE capability WHEN 'text' THEN 1 ...` 原来
在四个地方各写了一遍（`providers.all`、`providers.defaults`、
`model_center.registrations`、`model_center.policies`）。四处字面量一致的时候
谁都不会出事，出事的是**加第五类能力那一天**：改了三处漏一处，界面就有两屏
按不同顺序排，而"顺序"这种东西没人会去 diff——它不像字段名错了会报错，
它只是看起来"有点不对"。

顺序不再手抄，由 `models.CAPABILITIES` 的**键序**现推：那张表本来就是
"五类能力叫什么、什么顺序"的唯一出处（前端按它渲染，后端按它校验取值），
再抄一遍 CASE 就是第二个事实来源。
"""
import re

from models import BACKEND_ORDER, CAPABILITIES

# `case_order` 的 `column` 是唯一会进 SQL 拼接、且**不由参数化承载**的那一段
# （`WHEN` 的值都是本模块的常量）。今天四个调用点全传字面量，但函数签名对外开放，
# 将来有人把请求参数透传进来就是一条注入口——所以在这里把它收成白名单。
# 允许的形状只有"标识符"或"表别名.标识符"（`alias.capability` 这种用法已经存在）。
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
# 值只允许"词/下划线/连字符"：这些取值来自本文件的常量表，不是外部输入，
# 但同一个函数将来被复用时不希望它成为拼接任意字符串的口子。
_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def case_order(column: str, values) -> str:
    """把一组取值按给定顺序翻译成 SQL 的 CASE 表达式。

    不在名单里的值沉到最后（`ELSE n+1`）而不是排到最前：库里躺着一条脏值时，
    它应该出现在列表末尾让人看见，而不是插队在第一行冒充"最常用的那一档"。
    """
    if not isinstance(column, str) or not _COLUMN_RE.match(column):
        raise ValueError(f"排序列名不合法：{column!r}")
    items = list(values)
    for v in items:
        if not isinstance(v, str) or not _VALUE_RE.match(v):
            raise ValueError(f"排序取值不合法：{v!r}")
    whens = " ".join(f"WHEN '{v}' THEN {i + 1}" for i, v in enumerate(items))
    return f"CASE {column} {whens} ELSE {len(items) + 1} END"


def capability_order(column: str = "capability") -> str:
    return case_order(column, CAPABILITIES)


def backend_order(column: str = "backend") -> str:
    """按 backend 排序。用 `BACKEND_ORDER`（含 remote）而不是展示用的 `BACKENDS`。

    这两张表（`provider_config`／`model_registration`）的 backend 都收 remote，
    拿两档的字典生成 CASE 会让 remote 行落到 `ELSE`——排在最后、看起来像脏值。
    """
    return case_order(column, BACKEND_ORDER)
