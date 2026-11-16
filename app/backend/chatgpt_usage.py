"""汇总本人在本平台发起的草案请求。历史缺失的指标保留为空。"""
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Literal, TypedDict

import chatgpt_plan

ZONE = ZoneInfo('Asia/Shanghai')
_UsageState = Literal['succeeded', 'failed', 'unconfirmed', 'generating']
STATES: tuple[_UsageState, ...] = ('succeeded', 'failed', 'unconfirmed', 'generating')


class _DailyUsage(TypedDict):
    date: str
    total: int
    succeeded: int
    failed: int
    unconfirmed: int
    generating: int


def _number(value: object) -> int | float | None:
    if (type(value) is int or type(value) is float) and value >= 0:
        return value
    return None


def _state(draft, now) -> _UsageState:
    if draft.get('state') == 'ready':
        return 'succeeded'
    if draft.get('metrics', {}).get('response_state') == 'unconfirmed':
        return 'unconfirmed'
    if draft.get('state') == 'generating':
        return 'unconfirmed' if now - draft['created_at'] > 180 else 'generating'
    return 'failed'


def summarize(user_id, days, project_ids, *, now=None):
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, ZONE).date()
    first = today - timedelta(days=days - 1)
    daily: dict[str, _DailyUsage] = {
        str(first + timedelta(days=i)): {'date': str(first + timedelta(days=i)),
        'total': 0, 'succeeded': 0, 'failed': 0, 'unconfirmed': 0, 'generating': 0}
        for i in range(days)}
    counts = dict.fromkeys(STATES, 0)
    models: dict[str, int] = {}
    durations: list[int | float] = []
    inputs: list[int] = []
    outputs: list[int] = []
    skipped = 0
    total = 0
    # 文件由草案入口原子替换；本目录不读取账号凭据、不逐条查询项目权限。
    for path in chatgpt_plan._directory().glob(f'draft-{int(user_id)}-*.json'):
        try:
            draft = json.loads(path.read_text())
        except (OSError, ValueError):
            skipped += 1
            continue
        if project_ids is not None and draft.get('project_id') not in project_ids:
            continue
        stamp = _number(draft.get('created_at'))
        if stamp is None or stamp > now:
            continue
        day = str(datetime.fromtimestamp(stamp, ZONE).date())
        if day not in daily:
            continue
        state = _state(draft, now)
        total += 1
        counts[state] += 1
        daily[day]['total'] += 1
        daily[day][state] += 1
        model = draft.get('model') or '未知模型'
        models[model] = models.get(model, 0) + 1
        duration = _number(draft.get('duration_ms'))
        if duration is not None:
            durations.append(duration)
        metrics = draft.get('metrics') or {}
        for field, values in (('input_tokens', inputs), ('output_tokens', outputs)):
            value = metrics.get(field)
            if type(value) is int and value >= 0:
                values.append(value)
    return {'days': days, 'timezone': 'Asia/Shanghai', 'total': total, **counts,
            'input_tokens': sum(inputs) if inputs else None,
            'output_tokens': sum(outputs) if outputs else None,
            'input_samples': len(inputs), 'output_samples': len(outputs),
            'avg_duration_ms': round(sum(durations) / len(durations)) if durations else None,
            'duration_samples': len(durations), 'skipped_records': skipped,
            'daily': list(daily.values()),
            'models': [{'model': model, 'total': count} for model, count in
                       sorted(models.items(), key=lambda row: (-row[1], row[0]))]}
