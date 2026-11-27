"""汇总本人在本平台发起的 ChatGPT 请求（含计划草案与图片反推）。历史缺失的指标保留为空。"""
import itertools
import json
import math
import time
import uuid
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
    if (type(value) is int or type(value) is float) and math.isfinite(value) and value >= 0:
        return value
    return None


def _state(draft: dict, now: float) -> _UsageState:
    if draft.get('state') == 'ready':
        return 'succeeded'
    raw_metrics = draft.get('metrics')
    if isinstance(raw_metrics, dict) and raw_metrics.get('response_state') == 'unconfirmed':
        return 'unconfirmed'
    if draft.get('state') == 'generating':
        created_at = _number(draft.get('created_at'))
        return 'unconfirmed' if (created_at is None or now - created_at > 180) else 'generating'
    return 'failed'


def record(user_id: int, project_id: int | None, model: str, *, state: str,
           source: str = 'asset_vision', duration_ms: int | None = None,
           metrics: dict | None = None, created_at: float | None = None) -> None:
    """记录非草案通道（如图片反推提示词）通过个人 ChatGPT 账号发起的请求用量。"""
    entry: dict[str, object] = {
        'project_id': project_id,
        'created_at': time.time() if created_at is None else created_at,
        'state': state,
        'model': model or '未知模型',
        'source': source,
    }
    if duration_ms is not None and duration_ms >= 0:
        entry['duration_ms'] = duration_ms
    if metrics:
        clean_metrics: dict[str, object] = {}
        if isinstance(metrics.get('response_state'), str):
            clean_metrics['response_state'] = metrics['response_state']
        for field in ('input_tokens', 'output_tokens'):
            value = metrics.get(field)
            if type(value) is int and value >= 0:
                clean_metrics[field] = value
        if clean_metrics:
            entry['metrics'] = clean_metrics
    try:
        chatgpt_plan._write(f'usage-{int(user_id)}-{uuid.uuid4()}', entry)
    except OSError:
        # 用量记账失败不应阻断业务推理本身。
        pass


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
    # 文件由各通道原子替换；本目录不读取账号凭据、不逐条查询项目权限。
    base_dir = chatgpt_plan._directory()
    records = itertools.chain(
        base_dir.glob(f'draft-{int(user_id)}-*.json'),
        base_dir.glob(f'usage-{int(user_id)}-*.json'),
    )
    for path in records:
        try:
            draft = json.loads(path.read_text())
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(draft, dict):
            skipped += 1
            continue
        if project_ids is not None and draft.get('project_id') not in project_ids:
            continue
        stamp = _number(draft.get('created_at'))
        if stamp is None or stamp > now:
            continue
        try:
            day = str(datetime.fromtimestamp(stamp, ZONE).date())
        except (OverflowError, OSError, ValueError):
            skipped += 1
            continue
        if day not in daily:
            continue
        state = _state(draft, now)
        total += 1
        counts[state] += 1
        daily[day]['total'] += 1
        daily[day][state] += 1
        raw_model = draft.get('model')
        model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else '未知模型'
        models[model] = models.get(model, 0) + 1
        duration = _number(draft.get('duration_ms'))
        if duration is not None:
            durations.append(duration)
        raw_metrics = draft.get('metrics')
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
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
