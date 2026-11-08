"""HTTP 层的错误码：给"被拒绝"的请求一个稳定的机器可读标识。

## 为什么需要它

现在错误响应只有 `detail` 一句话，而且有 **两套形状**并存：

  * 业务拒绝（手写 `HTTPException`）：`{"detail": "作业不存在"}` —— detail 是**字符串**
  * 校验拒绝（Pydantic/FastAPI 默认）：`{"detail": [{"type":..., "loc":..., "msg":...}]}`
    —— detail 是**数组**

前端 `api.js` 的写法是 `let detail = JSON.parse(text).detail ?? text; new Error(detail)`。
数组进 `new Error()` 之后会被 JS 当作 `Array.prototype.toString()`，
结果显示成 **`[object Object]`** —— 用户看到的就是这五个字，没有任何可用信息。

这是"两套形状"的真实代价，不是理论洁癖。修法不是让前端学会分支，
而是**让后端只发一种形状**：`detail` 恒为字符串（人看的），
另给 `code`（机器看的）。

## 三条纪律

1. **`detail` 永远是字符串**。不管上游给的是数组、dict 还是 Exception，
   handler 负责压成一句话。前端的既有写法因此不用改。
2. **`code` 是稳定的**，前端的 `switch` 可以依赖它；`detail` 是可以改文案的。
3. **只加字段、不改既有字段**。`detail` 的内容尽量保持原样（尤其 5xx 里
   带排障信息的那几句），因为浏览器验收与说明书里引用过这些句子。

## 与 `error_catalog` 的关系（重要）

**两者不合并**：

  * `error_catalog` 答的是「一条**作业/产线节点**失败后，人该怎么理解与处理」
    —— 七字段诊断（label/explain/technical/impact/action/retryable/may_cost），
    面向的是"已经发生的一次执行"，写进 job.error_code 与节点输出。
  * 本模块答的是「一次 **HTTP 请求**为什么被拒」—— 面向的是"这一次调用"，
    不改任何作业状态。

它们的重叠只有"错误码应该只有一份清单"这一点，而目前两边的取值域并不重合
（HTTP 拒绝是 `not_found`/`forbidden`/`conflict` 这类；执行失败是
`adapter_*`/`provider_*`/`run_timeout` 这类）。强行合并会把两个不同生命周期
的东西塞进同一张表，改一个动另一个。**共用的是 `code` 的命名风格，不是表。**
"""
from __future__ import annotations

# 状态码 → 默认错误码。业务代码不写 code 时按这张表兜底，
# 这样"没有任何一个端点需要为了有 code 而改一行"。
BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_payload",
    423: "locked",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
    507: "insufficient_storage",
}

# 校验错误里常见的 Pydantic type → 一句中文。
# 不追求覆盖全部（Pydantic 的 type 有几十种），只保证最常见的那几种
# 有让人看得懂的说法；不认识的退回原文的 `msg`（英文，但至少比 [object Object] 强）。
VALIDATION_HINTS: dict[str, str] = {
    "missing": "缺少必填项",
    "string_type": "要填文本",
    "int_type": "要填整数",
    "float_type": "要填数字",
    "bool_type": "要填是/否",
    "list_type": "要填一组值",
    "dict_type": "要填一个对象",
    "literal_error": "取值不在允许范围里",
    "string_too_short": "内容太短",
    "string_too_long": "内容太长",
    "greater_than": "数值太小",
    "less_than": "数值太大",
    "none_required": "这一项不能有值",
}


def code_for_status(status: int) -> str:
    """状态码对应的默认错误码。表里没有的给 `http_<状态码>`，保持可读。"""
    return BY_STATUS.get(status, f"http_{status}")


def detail_text(detail) -> str:
    """把任意 `detail` 压成**一句人话**。这是保证"detail 恒为字符串"的那一道。"""
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, (list, tuple)):
        parts = [_validation_part(item) for item in detail]
        return "；".join(p for p in parts if p)
    if isinstance(detail, dict):
        # Pydantic v2 偶见 {"type":..., "msg":...} 直接给出来
        return _validation_part(detail)
    return str(detail)


def _validation_part(item) -> str:
    """一条校验错误 → 「字段 X：缺什么」。`loc` 去掉开头的 body/query/path 噪音。"""
    if not isinstance(item, dict):
        return str(item)
    loc = item.get("loc") or []
    if isinstance(loc, (list, tuple)):
        # 第一段通常是 body/query/path 之一，对人没有增量信息，去掉。
        # 剩下的用 `.` 拼成 `payload.name` 这种可读路径。
        rest = [str(p) for p in loc[1:]] if len(loc) > 1 else [str(p) for p in loc]
        field = ".".join(rest)
    else:
        field = str(loc)
    hint = VALIDATION_HINTS.get(str(item.get("type") or ""), "")
    msg = hint or str(item.get("msg") or "")
    if field and msg:
        return f"{field}：{msg}"
    return field or msg


def payload(status: int, detail, code: str | None = None) -> dict:
    """错误响应的唯一形状：`detail` 恒为字符串，另给 `code`。"""
    return {"detail": detail_text(detail), "code": code or code_for_status(status)}
