"""异常恢复目录（任务书 §14，P1 第 7 项）。

这一层的全部风险是**说谎**：把不认识的码套进一个通用文案，人就照着做，
而"可重试"这一栏猜错的代价可能是再扣一次钱。所以用例按三条来写：

1. §14 点名的十二类异常，每一类都要有能对上的条目（按标题匹配，不按条数——
   条数会随新增变化，写死条数等于给下一轮埋一个假失败）；
2. 每条都必须把七个字段填齐（缺一栏就是界面会显示 `undefined` 的那一类缺陷）；
3. 认不出来的码必须**承认认不出来**，并且不给"可重试"。
"""
import pathlib

import error_catalog as catalog
import worker

# 任务书 §14 列的十二类，按目录标题里的关键词找
REQUIRED = ["模型不可用", "引擎异常", "Worker 离线", "本地资源不足", "依赖缺失",
            "生成任务超时", "云端结果未知", "文件写入失败", "素材文件不见",
            "审核签字已过期", "源内容已变", "合成前检查没过"]


def _row(**over):
    base = {"id": 1, "status": "failed", "error_code": "run_timeout",
            "error_message": "执行超过上限 5 秒，已终止", "execution_mode": "local",
            "billable_status": "known", "provider_request_id": ""}
    base.update(over)
    return base


def test_十四的十二类异常都有对应条目():
    labels = "｜".join(e["label"] for e in catalog.CATALOG.values())
    missing = [need for need in REQUIRED if need not in labels]
    assert not missing, f"目录没盖住这几类：{missing}"


def test_每条目录项七个字段齐且布尔栏真是布尔():
    for code, entry in catalog.CATALOG.items():
        for field in catalog.ENTRY_FIELDS:
            assert field in entry, f"{code} 少一栏：{field}"
            assert str(entry[field]).strip(), f"{code} 的 {field} 是空的"
        for flag in ("retryable", "may_cost", "needs_human"):
            assert isinstance(entry[flag], bool), f"{code} 的 {flag} 不是布尔"


def test_认不出的码承认认不出且不给可重试():
    view = catalog.lookup("brand_new_code")
    assert view["known"] is False
    assert view["retryable"] is False
    # 原话仍要带出去（界面显示它，不显示编出来的解释）
    assert "未登记" in view["label"]


def test_云端结果未知一律不给直接重试():
    got = catalog.for_job(_row(status="needs_attention", execution_mode="cloud",
                               error_code="provider_unavailable",
                               billable_status="pending_reconciliation",
                               provider_request_id="req-9"))
    assert got["uncertain"] is True
    assert got["diagnosis"]["retryable"] is False, "结果未知时最坏的建议就是替人按重试"
    assert got["diagnosis"]["needs_human"] is True
    assert got["diagnosis"]["may_cost"] is True
    assert got["provider_request_id"] == "req-9"
    assert "云端结果未知" in got["diagnosis"]["label"]


def test_本地超时可重试且不花钱():
    got = catalog.for_job(_row())
    assert got["diagnosis"]["retryable"] is True
    assert got["diagnosis"]["may_cost"] is False
    assert got["diagnosis"]["known"] is True


def test_被驳回的产物不该原样重投():
    got = catalog.for_job(_row(error_code="review_rejected"))
    assert got["diagnosis"]["retryable"] is False
    assert "重做" in got["diagnosis"]["action"]


def test_成功的作业不谎报异常():
    got = catalog.for_job(_row(status="succeeded", error_code="", error_message=""))
    assert got["diagnosis"]["label"] == "这条作业没有异常"
    assert got["diagnosis"]["known"] is True


def test_接口把诊断给到作业详情(as_role, clean_queue, seeded):
    """走真接口：一条失败的 mock 作业（`mock_failed` 是个没进目录的码），
    界面上应当看到"未登记"而不是被编出来的解释。"""
    c = as_role("editor")
    job = c.post("/api/jobs", json={
        "project_id": seeded["project_id"], "capability": "video",
        "execution_mode": "local", "provider_code": "mock", "model_code": "mock-1",
        "prompt": "让它失败", "params": {"sleep": 0.05, "fail": True}}).json()
    assert worker.run_once("w-diag") == job["id"]

    detail = c.get(f"/api/jobs/{job['id']}").json()
    assert detail["status"] == "failed" and detail["error_code"] == "mock_failed"

    got = c.get(f"/api/jobs/{job['id']}/diagnosis").json()
    assert got["error_code"] == "mock_failed"
    assert got["diagnosis"]["known"] is False
    assert got["error_message"] == detail["error_message"], "原话必须一字不改地带出去"


def test_排队与执行中的作业不谎报错误():
    """这一条是写完接口才想起来的：`error_code` 对排队中的作业本来就是空的,
    而空码去查目录会命中"未登记的错误码"——界面就变成对着一正常排队的作业喊狼来了。
    """
    for status in ("queued", "starting", "running", "cancel_requested"):
        got = catalog.for_job(_row(status=status, error_code="", error_message=""))
        assert got["diagnosis"]["known"] is True, status
        assert "排队" in got["diagnosis"]["label"] or "执行" in got["diagnosis"]["label"], status
        assert got["diagnosis"]["retryable"] is False


def test_失败但没有错误码时说实话():
    got = catalog.for_job(_row(status="failed", error_code="", error_message="旧数据"))
    assert got["diagnosis"]["known"] is True
    assert "没有留下错误码" in got["diagnosis"]["label"]


def test_产线节点的每个失败码都在目录里():
    """节点失败码是 `_NodeFail("code", ...)` 这类**写在代码里的字符串**，
    没有枚举表可查，所以这一条按源码扫：新增一码而目录没跟上，这里就该变红。
    目录没跟上时的表现是界面对着一句执行器原话显示「未登记的错误码」——
    那是诚实的，但对第二次遇到同一件事的人没有用。
    """
    import re
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("pipeline.py").read_text()
    codes = set(re.findall(r'_NodeFail\(\s*"([a-z_]+)"', src))
    codes |= set(re.findall(r'finish_node\([^"]*NODE_FAILED,\s*error_code="([a-z_]+)"', src))
    assert codes, "扫不到任何节点失败码，说明这条用例的写法已随 pipeline.py 失效"
    missing = sorted(c for c in codes if c not in catalog.CATALOG)
    assert not missing, f"这些产线节点码没进目录：{missing}"


def test_节点诊断把重试闸门原样叠上():
    """目录只说「这类失败通常可重试」；这一格现在能不能点，只有 `retry_gate` 说了算。
    两处各判一次就会出现「钮在、点了吃 409」，所以 `retry_hint` 一非空就必须压住。"""
    node = {"id": 7, "node_type": "check_shots", "label": "检查镜头",
            "error_code": "no_shots", "error_message": "这一集一条镜头都没有"}
    open_dx = catalog.for_node(node)["diagnosis"]
    gated_dx = catalog.for_node(node, retry_hint="上一格还在跑")["diagnosis"]
    assert open_dx["known"] is True and open_dx["retryable"] is True
    assert gated_dx["retryable"] is False
    assert gated_dx["retry_hint"] == "上一格还在跑"
    # 文案仍来自目录，闸门只改「能不能点」这一栏
    assert gated_dx["explain"] == open_dx["explain"]


def test_没有失败的节点不硬套目录():
    node = {"id": 8, "node_type": "video_shot", "label": "生成镜头视频",
            "error_code": "", "error_message": ""}
    got = catalog.for_node(node)
    assert got["diagnosis"]["known"] is True
    assert "没有异常" in got["diagnosis"]["label"]
    assert got["diagnosis"]["retryable"] is False
