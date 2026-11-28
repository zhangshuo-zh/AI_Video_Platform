"""模型中心：登记、引擎视图、任务调用策略、场景候选。

一句话职责边界：**这一层只管"模型与引擎的配置与真相"，不管选路。**
选路仍然只有 `services.resolve_provider()` 一处，默认轨仍然只有
`provider_config.is_default` 一个来源（裁定 MC1＝备用方案只提示不自动切）。
所以本文件里没有任何接口会被提交路径自动采用，`/model-policies/resolve`
返回的候选只是给人看的，返回值里也明写 `auto_switch: false`。

配置接口沿用 `system:config`。`/model-policies/resolve/{scene}` 和
`/engines/comfyui/checkpoints` 为业务页面提供只读选项，登录即可访问。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import engine_launch
import engines
import model_center as mc
from services import model_configuration, model_operations, model_environment
from deps import require_permission, require_user
from repositories.engine_instances import engine_instances
from models import (ComfyUICheckpointsOut, ComfyUIInstanceOut, ConfigChangesOut,
                    EngineActionOut, EngineInstanceIn, EngineInstancePatch,
                    EngineListOut, EngineMutationOut, EngineProbeOut,
                    EngineProbesOut, ModelCatalogOut, ModelMutationOut,
                    ModelPoliciesOut, ModelPoliciesSaveOut, ModelPolicyIn,
                    ModelProbeOut, ModelRegistrationIn, ModelRegistrationPatch,
                    ModelResolveOut, ModelScenesOut, ModelStatsOut,
                    RuntimePathIn, RuntimePathsOut,
                    SecretProbeOut, SecretsOut,
                    SecretValueIn, SecretValueOut, SecretValuesOut,
                    ValidationOut)

router = APIRouter(prefix="/api", tags=["model-center"])


def _model_call(operation, *args, **kwargs):
    """统一把配置服务的领域错误映射成既有 HTTP 状态。"""
    try:
        return operation(*args, **kwargs)
    except model_configuration.ModelOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

# 验证表认哪些 backend。**不在这里数一遍**：0045 起 model_validation 的 CHECK
# 与登记层的 REG_SOURCES 是同一套取值（local／cloud／remote），第二份枚举就是
# 第二把会漂的尺子——这个文件里已经有过一次教训（见 test_枚举的说法只有一份出处）。
# 原来只认 local/cloud，于是 remote 登记"验不了也留不下痕"；那不是设计选择，
# 是表装不下那个值。0045 扩了 CHECK 之后两边取值相同。
_VERIFIABLE_BACKENDS = model_operations._VERIFIABLE_BACKENDS


def _json_list(raw) -> list:
    return model_configuration._json_list(raw)


# 破坏性接口的确认口令。原来是「confirm 必须等于中文字面量」——`撤登记`／`撤实例`
# 各是一个魔法词，从 curl 手工调用要 UTF-8 转义（`confirm=%E6%92%A4%E7%99%BB%E8%AE%B0`），
# 拼错一个字得到的是 422 而不是"你拼错了"这种信息；更糟的是它把界面文案变进了协议：
# 哪天改一句提示语，接口的入参也跟着变。
# 现在只认"人表达过一次确认"这件事本身：1／true／yes（大小写不敏感）。
# 语义边界不变——**这一枪仍然不能裸调**，只是不再要求一个中文词。
# 项目删除那条仍然是 `confirm=<项目名>`：那里比的是"你有没有打对名字"，
# 是防手滑的另一种强度（见 routers/projects.py），不入这一套。
_CONFIRM_WORDS = model_operations._CONFIRM_WORDS


def _confirmed(raw: str) -> bool:
    return _model_call(model_operations._confirmed, raw)


def _row_or_404(reg_id: int):
    return _model_call(model_operations._row_or_404, reg_id)


def _item_of(reg_id: int) -> dict:
    return model_configuration._item_of(reg_id)


@router.get("/models", response_model=ModelCatalogOut)
def list_models(_=Depends(require_permission("system:config"))) -> dict:
    return model_configuration.catalog_payload()


@router.get("/models/stats", response_model=ModelStatsOut)
def model_stats(_=Depends(require_permission("system:config"))) -> dict:
    """顶部四数。口径由后端一处给（caliber 字段），界面只念不算。"""
    return mc.stats()


@router.post("/models/probe", response_model=ModelProbeOut)
def probe_models(fresh: int = 0,
                 _=Depends(require_permission("system:config"))) -> dict:
    """现查一次服务型引擎，再把**同一份**目录与四数一起回给界面。

    为什么要有这一条：`GET /api/models` 从 2026-11-07 起**不出网**（详见
    `mc.catalog()` 的注解）——打开一页不该被一台卡住的服务拖住。于是"这一档
    到底连不连得上"必须有一个显式入口，这就是它：人点了「探测」或页面画完
    之后自己补这一枪，两种都等得起。

    `fresh=1`＝绕过 15 秒缓存（人明确要求现查）。页面画完自动补的那一枪用 0：
    刚在别处探过就直接复用，不为一屏之内重复问同一台机器。

    返回的是**整张目录**而不是几条健康：界面拿它整页重绘，字号、状态、顶部四数
    因此与列表逐行对得上；只回健康的话前端还得自己把状态拼一遍，那就是第二套口径。

    `cloud` 段是 2026-11-08 补的：按钮原来只现查本机服务型引擎，云端轨"查的是
    密钥变量在不在"，点一下按钮云端一格不动，用起来怪。现在云端也真去供应商
    网关问一次——但**结论口径不变**：探的是"路通不通、钥匙对不对"，永远不等于
    "那一档模型可用"。实现与纪律见 `cloud_probe.py` 模块头。
    """
    return mc.probe_all(fresh=bool(fresh))


@router.post("/models", response_model=ModelMutationOut, status_code=201)
def create_model(payload: ModelRegistrationIn,
                 user=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.create_model, payload, user)


@router.patch("/models/{reg_id}", response_model=ModelMutationOut)
def patch_model(reg_id: int, payload: ModelRegistrationPatch,
                _=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.patch_model, reg_id, payload)


@router.delete("/models/{reg_id}", response_model=ModelMutationOut)
def remove_model(reg_id: int, confirm: str = "",
                 user=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.remove_model, reg_id, confirm, user)


@router.post("/models/{reg_id}/check", response_model=ValidationOut)
def check_model(reg_id: int, user=Depends(require_permission("system:config"))):
    return _model_call(model_operations.check_model, reg_id, user)


@router.get("/model-policies", response_model=ModelPoliciesOut)
def list_policies(_=Depends(require_permission("system:config"))) -> dict:
    return model_configuration.policies_payload()


@router.put("/model-policies", response_model=ModelPoliciesSaveOut)
def save_policies(payload: list[ModelPolicyIn],
                  user=Depends(require_permission("system:config"))) -> dict:
    try:
        return model_configuration.save_policies(payload, user["id"])
    except model_configuration.ModelPolicyInvalid as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/config-changes", response_model=ConfigChangesOut)
def list_config_changes(limit: int = Query(50, ge=1),
                        _=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.list_config_changes, limit)


@router.get("/model-policies/scenes", response_model=ModelScenesOut)
def list_scenes(_=Depends(require_user)) -> dict:
    return model_configuration.scenes_payload()


@router.get("/model-policies/resolve/{scene_key}", response_model=ModelResolveOut, response_model_exclude_unset=True)
def resolve(scene_key: str, _=Depends(require_user)) -> dict:
    return _model_call(model_operations.resolve, scene_key)



def _tier_problem(capability: str, provider: str, model_code: str) -> str:
    return model_configuration._tier_problem(capability, provider, model_code)


# ================= 引擎实例（W-B／CR-001） =================
# 这一族接口管的是"这一档引擎在哪个端点"，不是"这台机器上跑了什么模型"。
# 三条边界与 0040 表头、`repositories/engine_instances.py` 的头一致：
#   * engine_code 必须在清单里——实例不能凭空发明一个 provider；
#   * 返回值里的健康只读探测缓存；现查由显式 POST 触发；
#   * 密钥只出现在"变量名＋有没有值"这一层，值永不进响应。

@router.get("/engines", response_model=EngineListOut, response_model_exclude_unset=True)
def list_engines(_=Depends(require_permission("system:config"))) -> dict:
    return model_configuration.engines_payload()


@router.post("/engines", response_model=EngineMutationOut, status_code=201)
def create_engine(payload: EngineInstanceIn,
                  user=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.create_engine, payload, user)


@router.get("/engines/comfyui/checkpoints", response_model=ComfyUICheckpointsOut)
def list_comfyui_checkpoints(instance_id: int | None = Query(None, gt=0, le=2**63 - 1),
                            _=Depends(require_user)) -> dict:
    return engines.comfyui_checkpoints(instance_id)


@router.get("/engines/comfyui/instances", response_model=list[ComfyUIInstanceOut])
def list_comfyui_instances(_=Depends(require_user)) -> list[dict]:
    return [{key: row[key] for key in ("id", "display_name", "endpoint")}
            for row in engine_instances.instances()
            if row["engine_code"] == "comfyui" and row["enabled"] == 1]


@router.patch("/engines/{instance_id}", response_model=EngineMutationOut)
def patch_engine(instance_id: int, payload: EngineInstancePatch,
                 _=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.patch_engine, instance_id, payload)


@router.delete("/engines/{instance_id}", response_model=EngineMutationOut)
def remove_engine(instance_id: int, confirm: str = "",
                  _=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.remove_engine, instance_id, confirm)


@router.get("/engines/{instance_id}/probes", response_model=EngineProbesOut)
def list_engine_probes(instance_id: int, limit: int = Query(20, ge=1),
                       _=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.list_engine_probes, instance_id, limit)


@router.post("/engines/{instance_id}/probe", response_model=EngineProbeOut)
def probe_engine(instance_id: int,
                 user=Depends(require_permission("system:config"))) -> dict:
    return _model_call(model_operations.probe_engine, instance_id, user)


@router.post("/engines/{instance_id}/launch", response_model=EngineActionOut)
def launch_engine(instance_id: int, request: Request,
                  user=Depends(require_permission("system:config"))) -> dict:
    client = request.client.host if request.client else ""
    if client not in engine_launch.LOOPBACK:
        raise HTTPException(
            status_code=403,
            detail="只能在运行平台的这台机器上启动本机引擎（当前请求来自 "
                   f"{client}）。请在服务器本机打开页面，或手动启动后回来点探测。")
    return _model_call(model_operations.launch_engine, instance_id, user)


@router.post("/engines/{instance_id}/stop", response_model=EngineActionOut)
def stop_engine(instance_id: int, request: Request,
                user=Depends(require_permission("system:config"))) -> dict:
    client = request.client.host if request.client else ""
    if client not in engine_launch.LOOPBACK:
        raise HTTPException(
            status_code=403,
            detail="只能在运行平台的这台机器上关闭本机引擎（当前请求来自 "
                   f"{client}）。请在服务器本机操作。")
    return _model_call(model_operations.stop_engine, instance_id, user)


def _age_seconds(stamp: str) -> int | None:
    return model_configuration._age_seconds(stamp)


def _last_probe_fields(row) -> dict:
    return model_configuration._last_probe_fields(row)


def _adapter_for(code: str):
    return model_configuration._adapter_for(code)


def _probe_warning(row, user_id: int | None = None) -> str:
    return _model_call(model_operations._probe_warning, row, user_id)


# ---------------------------------------------------------------- 本地运行时路径（0051）
#
# 为什么挂在模型中心这一组而不是新开一个"参数管理"一级菜单：
#   * 这 10 个变量**全部**是引擎/模型运行时相关的（见 `runtime_paths.LABELS`）；
#   * 引擎管理页已经在同一屏，人配完引擎顺手配路径，不必去第二个地方找；
#   * 一级菜单是稀缺资源（每次加都要教人"去哪个新页面"），不值得为 10 行配置开一个。
#
# 三条与密钥边界的硬关系：
#   * 这一组**只接受路径类变量**，白名单在 `repositories/runtime_path.ALLOWED_ENV`
#     与 migration 0051 的 CHECK 里各写一遍。任何 `*_API_KEY` / `*_TOKEN`
#     都进不来，两道各自独立。
#   * 密钥仍只从进程环境读（`adapters.py` 头第 3 条），这里一行都不碰。
#   * 响应里出现的路径是**本机文件系统路径**，只给 `system:config` 的人看。

@router.get("/runtime-paths", response_model=RuntimePathsOut)
def list_runtime_paths(_=Depends(require_permission("system:config"))
                       ) -> RuntimePathsOut:
    return _model_call(model_environment.list_runtime_paths)


def _runtime_state(env_name: str, effective: str | None) -> tuple[str, str]:
    return _model_call(model_environment._runtime_state, env_name, effective)


@router.put("/runtime-paths/{env_name}", response_model=RuntimePathsOut)
def save_runtime_path(env_name: str, payload: RuntimePathIn,
                      user=Depends(require_permission("system:config"))
                      ) -> RuntimePathsOut:
    return _model_call(model_environment.save_runtime_path, env_name, payload, user)


# ---------------------------------------------------------------- 密钥指纹与连通性（2026-11-11）
#
# 为什么需要（用户场景）：配置里可能同时有好几把 key（旧的没删、新加的、
# 按项目分开的），人在供应商后台看到一串 key 时**无法确认这台机器上配的是哪一把**，
# 而让他去 `cat ~/.zshrc` 看等于给了 shell。指纹解决的就是这一个问题。
#
# **这一组一个字符的密钥值都不返回。** 指纹是 `sha256(值)` 的前 8 位，
# 推不回原值；`preview` 只露出供应商的公开家族标记（`sk-`／`sk-ant-` 这类
# 公开文档里写着的名字），**不露值的任何随机段**——第一版写错成
# `value[:10]` 时实测把真实密钥前 12 位印在了页面上，已修（见 `_KEY_PREFIX_RE`）。
#
# 两条与重启的关系，必须在界面上一并说清（这是本组存在的一半理由）：
#   * 密钥由**环境变量**给，而 worker 是 fork 时拿到环境快照的独立进程：
#     改了 `.zshrc` 不重启 worker，云端作业仍会报「环境变量 X 没有值」；
#   * 但 **API 进程读的是实时的**（`adapters.key_present` 每次现读 `os.environ`），
#     所以这一页的「已配置／指纹」在导出后会立刻变，而作业那边没变——
#     这个错配是让人怀疑平台坏了的第一来源，页面上要直说。

def _cloud_key_items() -> list[dict]:
    return _model_call(model_environment._cloud_key_items)


@router.get("/secrets", response_model=SecretsOut)
def list_secrets(_=Depends(require_permission("system:config"))) -> SecretsOut:
    return _model_call(model_environment.list_secrets)


@router.post("/secrets/{provider}/test", response_model=SecretProbeOut)
def test_secret(provider: str, _=Depends(require_permission("system:config"))
                ) -> SecretProbeOut:
    return _model_call(model_environment.test_secret, provider)


# ------------------------------------------------ 密钥的密文存储（0052，2026-11-11）
#
# 用户批准实现 A 后的入口：**管理员可以在页面上配置供应商密钥**，
# 值以 AES-256-GCM 密文落库，KEK 只在环境变量 `PLATFORM_SECRET_KEK`。
#
# 三条边界（与 `secret_store.py` 文件头同一套）：
#   1. **响应里永远不含明文**。读回来只有指纹、长度、更新时间、来源。
#      "保存成功"也不回显值——回显就把它写进浏览器历史/截屏/日志。
#   2. **环境变量优先**。库里存的是"环境里没有时的值"，页面显示
#      `source: env/db/none` 让人一眼看出当前生效的是哪一份。
#   3. **KEK 缺失时功能整体不可用**，返回 409 并说清怎么生成，
#      而不是静默接受写入（那会写进一条将来解不开的行）。

def _secret_out(env_name: str) -> SecretValueOut:
    return _model_call(model_environment._secret_out, env_name)


@router.get("/secret-values", response_model=SecretValuesOut)
def list_secret_values(_=Depends(require_permission("system:config"))
                       ) -> SecretValuesOut:
    return _model_call(model_environment.list_secret_values)


@router.put("/secret-values/{env_name}", response_model=SecretValueOut)
def save_secret_value(env_name: str, payload: SecretValueIn,
                      user=Depends(require_permission("system:config"))
                      ) -> SecretValueOut:
    return _model_call(model_environment.save_secret_value, env_name, payload, user)


@router.delete("/secret-values/{env_name}", response_model=SecretValueOut)
def delete_secret_value(env_name: str,
                        _=Depends(require_permission("system:config"))
                        ) -> SecretValueOut:
    return _model_call(model_environment.delete_secret_value, env_name)
