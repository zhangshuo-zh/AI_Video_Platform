"""状态机表本身的自洽性：所有状态有去处、所有动作只引合法状态。"""
import pytest

import job_state as S


def test_每个状态都有去处():
    assert set(S.TRANSITIONS) == set(S.STATUSES)
    for old, targets in S.TRANSITIONS.items():
        assert targets, f"{old} 没有合法去处"
        for new in targets:
            assert new in S.STATUSES, f"{old} -> {new} 不是合法状态"
            assert new != old, f"{old} 的去处里不该有自己（幂等由调用方处理）"


def test_动作表只引合法状态与合法转移():
    for name, spec in S.ACTIONS.items():
        assert spec["from"], f"{name} 没有起点"
        assert spec["permission"], f"{name} 没声明权限"
        for old, new in spec["from"].items():
            assert old in S.STATUSES, f"{name}: 起点 {old} 不合法"
            assert new is None or new in S.STATUSES, f"{name}: 终点 {new} 不合法"
            # 重试是新建一行，原行不转移；对它的 to 不做转移表校验。
            if spec.get("new_row") or new is None or new == old:
                continue
            assert new in S.TRANSITIONS[old], f"{name}: {old} -> {new} 不在转移表里"


def test_取消只有活着的作业能点():
    for status in S.ACTIVE:
        assert S.can("cancel", status), f"{status} 应该能取消"
    for status in (S.SUCCEEDED, S.FAILED, S.CANCELLED, S.NEEDS_ATTENTION):
        assert not S.can("cancel", status), f"{status} 不该能取消"


def test_重试池就是三个可重试终态():
    for status in S.RETRYABLE:
        assert S.can("retry", status)
    for status in (S.SUCCEEDED, S.QUEUED, S.RUNNING):
        assert not S.can("retry", status)


def test_取消的目标状态按排队与在跑分开():
    assert S.target("cancel", S.QUEUED) == S.CANCELLED
    assert S.target("cancel", S.RUNNING) == S.CANCEL_REQUESTED
    # 已经请求过取消：再点一次仍落在 cancel_requested，不报错
    assert S.target("cancel", S.CANCEL_REQUESTED) == S.CANCEL_REQUESTED


def test_非法转移被挡住():
    S.assert_transition(S.QUEUED, S.STARTING)
    S.assert_transition(S.RUNNING, S.SUCCEEDED)
    S.assert_transition(S.SUCCEEDED, S.SUCCEEDED)      # 同一状态写两次是幂等
    with pytest.raises(ValueError):
        S.assert_transition(S.SUCCEEDED, S.RUNNING)


def test_未知动作一律不能做():
    assert not S.can("nope", S.QUEUED)
    assert S.target("nope", S.QUEUED) is None
    assert S.permission("nope") == ""


def test_状态表与文案表一一对应():
    """状态机加了一个状态、忘了加中文标签，界面会念出英文码。"""
    import models
    assert set(S.STATUSES) == set(models.JOB_STATUSES)
