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
from models import BACKENDS, CAPABILITIES


def case_order(column: str, values) -> str:
    """把一组取值按给定顺序翻译成 SQL 的 CASE 表达式。

    不在名单里的值沉到最后（`ELSE n+1`）而不是排到最前：库里躺着一条脏值时，
    它应该出现在列表末尾让人看见，而不是插队在第一行冒充"最常用的那一档"。
    """
    items = list(values)
    whens = " ".join(f"WHEN '{v}' THEN {i + 1}" for i, v in enumerate(items))
    return f"CASE {column} {whens} ELSE {len(items) + 1} END"


def capability_order(column: str = "capability") -> str:
    return case_order(column, CAPABILITIES)


def backend_order(column: str = "backend") -> str:
    return case_order(column, BACKENDS)
