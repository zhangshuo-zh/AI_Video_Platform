"""参数模板接口（W08）。

模板只解决"同一个镜头不要每次重填参数"，不参与执行、不改历史 Job。
两条权限边界：
  * 全局模板（project_id 为空）只有管理员能写，所有人能读；
  * 项目模板按项目归属判，读要 read、写要 write。
名字在同一范围 + 同一能力下不重复；同一范围 + 同一能力只有一个默认模板。
"""
import json
import re  # noqa: F401  (保留：密钥键名的白名单正则就在下面)

from fastapi import APIRouter, Depends, HTTPException, status as http_status

import access
import adapters
from deps import require_permission
from models import (GenerationProfileIn, GenerationProfileOut,
                    GenerationProfileUpdateIn)
from repositories.profiles import profiles
from repositories.projects import projects as project_repo

router = APIRouter(prefix="/api", tags=["profiles"])

# 模板参数里绝不允许夹带凭据。密钥只走环境变量名（W06 的 key_env），
# 这里出现 api_key/token 之类，说明有人在把模板当配置表用——直接拒绝。
_SECRET_KEY = re.compile(
    r"(^|_)(api_?key|access_?key|secret|password|authorization|token)$", re.I)
_MAX_PARAM_BYTES = 4000


def _check_params(params: dict) -> None:
    if not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="params 必须是一个对象")
    raw = json.dumps(params, ensure_ascii=False)
    if len(raw.encode("utf-8")) > _MAX_PARAM_BYTES:
        raise HTTPException(status_code=422, detail="模板参数太大（上限 4KB）")

    def walk(value, depth=0):
        if depth > 4:
            raise HTTPException(status_code=422, detail="模板参数嵌套太深")
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET_KEY.search(str(key)):
                    raise HTTPException(status_code=422,
                                        detail=f"模板参数里不能放凭据：{key}")
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

    walk(params)


def _adapter(capability: str, provider: str):
    for mode in ("local", "cloud"):
        found = adapters.find(capability, mode, provider)
        if found is not None:
            return found
    return None


def _check_route(capability: str, provider: str, model: str, params: dict) -> None:
    """provider/model 要么都不填（通用模板），要么一起填且必须对得上清单。

    参数只在绑定到具体模型时才能校验；不绑定却带参数，等于跳过白名单，
    所以这一档只允许空参数。
    """
    provider, model = provider.strip(), model.strip()
    if not provider and not model:
        if params:
            raise HTTPException(status_code=422,
                                detail="不绑定 provider/模型时不能带参数；否则无法按清单校验")
        return
    if not provider or not model:
        raise HTTPException(status_code=422,
                            detail="provider 与模型要么都不填，要么一起填")
    adapter = _adapter(capability, provider)
    if adapter is None:
        raise HTTPException(status_code=422,
                            detail=f"provider「{provider}」没有登记适配器，模板不能绑它")
    errors = adapters.validate_params(adapter, model, params)
    if errors:
        raise HTTPException(status_code=422, detail="；".join(errors))


def _scope_ids(user) -> list[int] | None:
    if access.is_manager(user):
        return None
    return [r["id"] for r in project_repo.list_visible(user["id"], False, "all")]


def _guard_write(row, user) -> None:
    if row["project_id"] is None:
        if not access.is_manager(user):
            raise HTTPException(status_code=403, detail="全局模板只有管理员能修改")
        return
    access.ensure(user, row["project_id"], "write")


@router.get("/generation-profiles", response_model=list[GenerationProfileOut])
def list_profiles(project_id: int | None = None, capability: str | None = None,
                  user=Depends(require_permission("job:read"))) -> list[GenerationProfileOut]:
    """列表里始终包含全局模板。带 project_id 时只看这个项目 + 全局；
    不带时按可见项目汇总（管理员看全部）。"""
    if project_id is not None:
        access.ensure(user, project_id, "read")
        ids = [project_id]
    else:
        ids = _scope_ids(user)
    return [GenerationProfileOut.from_row(r)
            for r in profiles.list(ids, capability)]


@router.post("/generation-profiles", response_model=GenerationProfileOut,
             status_code=http_status.HTTP_201_CREATED)
def create_profile(payload: GenerationProfileIn,
                   user=Depends(require_permission("job:submit"))) -> GenerationProfileOut:
    if payload.project_id is None:
        if not access.is_manager(user):
            raise HTTPException(status_code=403, detail="全局模板只有管理员能建")
    else:
        access.ensure(user, payload.project_id, "write")

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="模板名不能为空")
    _check_params(payload.params)
    _check_route(payload.capability, payload.provider_code, payload.model_code,
                 payload.params)
    if profiles.name_taken(payload.project_id, payload.capability, name):
        raise HTTPException(status_code=409, detail="这个范围里已经有同名模板")

    return GenerationProfileOut.from_row(profiles.create({
        "project_id": payload.project_id, "name": name,
        "capability": payload.capability,
        "provider_code": payload.provider_code.strip(),
        "model_code": payload.model_code.strip(),
        "params": payload.params, "is_default": payload.is_default,
        "created_by": user["id"],
    }))


@router.put("/generation-profiles/{profile_id}", response_model=GenerationProfileOut)
def update_profile(profile_id: int, payload: GenerationProfileUpdateIn,
                   user=Depends(require_permission("job:submit"))) -> GenerationProfileOut:
    row = profiles.get(profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    _guard_write(row, user)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="模板名不能为空")
    _check_params(payload.params)
    _check_route(row["capability"], row["provider_code"], row["model_code"],
                 payload.params)
    if profiles.name_taken(row["project_id"], row["capability"], name,
                           exclude_id=profile_id):
        raise HTTPException(status_code=409, detail="这个范围里已经有同名模板")

    updated = profiles.update(profile_id, name, payload.params, payload.is_default)
    if updated is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return GenerationProfileOut.from_row(updated)


@router.delete("/generation-profiles/{profile_id}",
               status_code=http_status.HTTP_204_NO_CONTENT)
def delete_profile(profile_id: int,
                   user=Depends(require_permission("job:submit"))) -> None:
    row = profiles.get(profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    _guard_write(row, user)
    profiles.delete(profile_id)
