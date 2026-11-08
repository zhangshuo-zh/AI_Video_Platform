"""`JobRepository` 的 mixin 组合层。

## 为什么拆

`repositories/jobs.py` 原本是单个 57KB 的 `JobRepository` 类（35 个方法），
把四件不同的事混在一起：作业 CRUD、队列租约与心跳、状态流转与人工核对、
成本汇总与队列健康。改其中一件要在一个 1100 行的类里滚很久。

## 怎么拆

按**职责**拆成 7 个 mixin，再组合成同名类：

    common  共享常量与模块级工具（谁都能依赖，它谁也不依赖）
    crud    建行、读单条/批量、列表、可见范围
    query   "按镜取最新一条"这类派生查询
    lease   领取、续租、心跳、恢复、worker 状态
    state   成功/失败/取消/待核对/人工核对/取消请求
    retry   重试、重试链、重试次数
    cost    成本汇总、项目统计
    health  队列健康读数

## 组合顺序有讲究

    class JobRepository(Crud, Query, Lease, State, Retry, Cost, Health, Common?)

`Cost` 依赖 `Crud` 的 `_scope()`（唯一的跨 mixin 调用），所以 `Crud` 必须在前。
其余 mixin 之间**零互相调用**（已用 AST 核对：35 个方法里只有 8 处 self 调用，
其中 7 处是 `_resources`/`_scope`/`_latest_by_shot` 这类同组内部调用）。

`common` 不是 mixin（没有方法），它的名字通过模块级 import 进入各 mixin。

## `jobs` 单例

API 完全不变：`from repositories.jobs import jobs` 仍然拿到同一个
`JobRepository()` 实例，`jobs.claim_next(...)`、`jobs.cost_summary(...)` 照旧。
"""

from __future__ import annotations

from .cost import JobsCostMixin
from .crud import JobsCrudMixin
from .health import JobsHealthMixin
from .lease import JobsLeaseMixin
from .query import JobsQueryMixin
from .retry import JobsRetryMixin
from .state import JobsStateMixin


class JobRepository(
    JobsCrudMixin,
    JobsQueryMixin,
    JobsLeaseMixin,
    JobsStateMixin,
    JobsRetryMixin,
    JobsCostMixin,
    JobsHealthMixin,
):
    """作业仓储。方法体全部在各 mixin 里，这里只是把它们组合起来。

    组合顺序与原来的单类方法定义顺序一致（crud → query → lease → state →
    retry → cost → health），这样 `inspect.getsource` 的比对与 MRO 都最直观。
    """
