"""智能体中心的 ChatGPT 制作计划草案。每次外发与创建运行都由用户明确发起。"""
import hashlib
import json
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import access
import agents
import agent_tools
import chatgpt_plan as chatgpt
import chatgpt_usage
from deps import require_permission
from models import (ChatGPTAcceptOut, ChatGPTConnectionOut, ChatGPTDraftOut,
                    ChatGPTModelsOut, ChatGPTUsageOut)
from repositories.content import episodes, shots

router = APIRouter(prefix='/api/agent/chatgpt', tags=['agents'])


class LoginIn(BaseModel):
    new_account: bool = False


class DraftIn(BaseModel):
    request_id: UUID
    project_id: int
    episode_id: int | None = None
    goal: str = Field(min_length=4, max_length=2000)
    model: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]


class AcceptIn(BaseModel):
    approval_valid_seconds: Literal[3600, 21600, 86400, 604800] = 86400


def _error(exc):
    return HTTPException(422, str(exc))


@router.get('/connection', response_model=ChatGPTConnectionOut)
def connection(user=Depends(require_permission('job:submit'))):
    return chatgpt.status(user['id'])


@router.post('/authorize', response_model=ChatGPTConnectionOut)
def authorize(payload: LoginIn, request: Request,
              user=Depends(require_permission('job:submit'))):
    # 浏览器必须与后端在同一台电脑，授权回调才可到达 loopback listener。
    if request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
        raise HTTPException(422, '请在部署平台的电脑上打开本地地址连接 ChatGPT')
    return chatgpt.start_login(user['id'], payload.new_account)


@router.delete('/connection', response_model=ChatGPTConnectionOut)
def disconnect(user=Depends(require_permission('job:submit'))):
    return chatgpt.disconnect(user['id'])


@router.get('/models', response_model=ChatGPTModelsOut)
def model_list(user=Depends(require_permission('job:submit'))):
    try:
        return {'items': chatgpt.models(user['id'])}
    except chatgpt.ChatGPTError as exc:
        raise _error(exc) from None


def _view(draft):
    out = {k: draft[k] for k in ('request_id', 'state', 'goal', 'model', 'error', 'run_id') if k in draft}
    if 'steps' in draft:
        labels = agent_tools.labels()
        out['steps'] = [{**step, 'tool_label': labels[step['tool']]['label'],
                         'risk_label': labels[step['tool']]['risk_label']} for step in draft['steps']]
    return out


@router.post('/drafts', response_model=ChatGPTDraftOut)
def generate(payload: DraftIn, user=Depends(require_permission('job:submit'))):
    access.ensure(user, payload.project_id, 'read')
    episode = episodes.get(payload.episode_id) if payload.episode_id else None
    if payload.episode_id and (episode is None or episode['project_id'] != payload.project_id):
        raise HTTPException(422, '所选集不属于当前项目')
    name = chatgpt.draft_name(user['id'], str(payload.request_id))
    fields = payload.model_dump(mode='json')
    fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    # 分配请求意图后立刻释放锁。网络、模型等待期间无需持锁。
    with chatgpt._lock(name):
        existing = chatgpt._read(name)
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise HTTPException(409, '此请求编号已用于另一个目标，请新建草案')
            return _view(existing)
        draft = {**fields, 'fingerprint': fingerprint, 'state': 'generating', 'created_at': time.time()}
        chatgpt._write(name, draft)
    started = time.monotonic()
    metrics: dict = {}
    try:
        if payload.model not in {row['id'] for row in chatgpt.models(user['id'])}:
            raise chatgpt.ChatGPTError('所选模型当前不可用，请刷新模型列表')
        prompt = agents.plan_prompt(payload.goal, episode,
            {'shots': len(shots.list_by_episode(episode["id"])) if episode else 0})
        text = chatgpt.infer(user['id'], payload.model, prompt, metrics=metrics)
        draft.update(state='ready', steps=agents.parse_plan(text))
    except (chatgpt.ChatGPTError, agents.AgentInvalid) as exc:
        draft.update(state='failed', error=str(exc))
    finally:
        draft['duration_ms'] = round((time.monotonic() - started) * 1000)
        draft['metrics'] = metrics
        # 失败和断流也记住请求，HTTP 重试不会重复消耗套餐。
        with chatgpt._lock(name):
            chatgpt._write(name, draft)
    return _view(draft)


def _load(user, key):
    name = chatgpt.draft_name(user['id'], str(key))
    draft = chatgpt._read(name)
    if not draft:
        raise HTTPException(404, '没有这份计划草案')
    access.ensure(user, draft['project_id'], 'read')
    return name, draft


@router.get('/drafts/{key}', response_model=ChatGPTDraftOut)
def draft_status(key: UUID, user=Depends(require_permission('job:submit'))):
    _, draft = _load(user, key)
    if draft['state'] == 'generating' and time.time() - draft['created_at'] > 180:
        return {**_view(draft), 'error': '上次生成未确认完成，可能由服务重启或连接中断导致。请检查套餐用量后决定是否重新生成。'}
    return _view(draft)


@router.post('/drafts/{key}/accept', response_model=ChatGPTAcceptOut)
def accept(key: UUID, payload: AcceptIn, user=Depends(require_permission('job:submit'))):
    try:
        run_id = chatgpt.accept_draft(
            user['id'], str(key), payload.approval_valid_seconds,
            ensure_access=lambda pid: access.ensure(user, pid, 'read'))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except chatgpt.DraftNotReadyError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'run_id': run_id, 'note': '已创建暂停的运行。查看步骤后，可在运行详情中恢复执行。'}


@router.get('/usage', response_model=ChatGPTUsageOut)
def usage(days: int = Query(7, ge=1, le=30), project_id: int | None = None,
          user=Depends(require_permission('job:submit'))):
    if days not in (7, 30):
        raise HTTPException(422, '统计范围支持最近 7 天或 30 天')
    if project_id is not None:
        access.ensure(user, project_id, 'read')
        project_ids: set[int] | None = {project_id}
    else:
        project_ids = access.visible_project_ids(user)
    return chatgpt_usage.summarize(user['id'], days, project_ids)
