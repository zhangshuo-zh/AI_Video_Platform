from typing import Literal

from pydantic import BaseModel, Field

from .enums import Capability, ConnectionType, HostingSource, PolicyPreference


# ---------- 模型中心（0035） ----------
# 读侧一律返回 dict：目录是"清单 × 登记"的联表，字段随清单走，
# 套一层 Pydantic 只会让加一个清单字段要改两处（manifest 端点早就这么定了）。
# 写侧必须有模型：校验发生在入库之前，不能让脏键名进库再靠界面挡。

REG_SOURCES: dict[str, str] = {"local": "本地", "cloud": "云端", "remote": "远程自建"}


POLICY_PREFERENCES: dict[str, str] = {"local": "优先本地", "cloud": "优先云端",
                                      "remote": "优先远程自建", "any": "不限"}




class ModelRegistrationIn(BaseModel):
    """新增登记。provider/model_code 是**指向清单的键**，不是自由文本：
    路由层会拿 adapters.find() 比一遍，清单里没有就要求显式确认。"""
    capability: Capability
    backend: HostingSource
    provider: str
    model_code: str
    display_name: str = ""
    # 空串＝"跟着 backend 走"。默认写死 "local" 会让云端登记在仓库页显示成
    # 本地——界面只传 backend，source 是给人看的第二口径，不该和它打架。
    source: HostingSource | Literal[""] = ""
    engine_code: str = ""
    version: str = ""
    artifact_path: str = ""
    remote_model_id: str = ""
    capabilities: list[str] = []
    enabled: bool = True
    note: str = ""




class ModelRegistrationPatch(BaseModel):
    """逐字段更新：没出现的键不动。"""
    display_name: str | None = None
    source: HostingSource | Literal[""] | None = None
    engine_code: str | None = None
    version: str | None = None
    artifact_path: str | None = None
    remote_model_id: str | None = None
    capabilities: list[str] | None = None
    enabled: bool | None = None
    note: str | None = None




class EngineInstanceIn(BaseModel):
    """新增引擎实例。只收配置字段——健康、版本、探测时间是现查值，
    在这里开字段就等于邀请人把它们写进库（0040 表头把这条边界写死了）。"""
    engine_code: str
    endpoint: str
    display_name: str = ""
    connection_type: ConnectionType = "local_http"
    shared_secret_env: str = ""
    enabled: bool = True
    note: str = ""




class EngineInstancePatch(BaseModel):
    """逐字段更新。`engine_code` 与 `endpoint` 是这一行的键，改不了——
    与登记弹窗那两格同一口径：改键＝换一台机器，应该新建一条再停旧的。"""
    display_name: str | None = None
    connection_type: ConnectionType | None = None
    shared_secret_env: str | None = None
    enabled: bool | None = None
    note: str | None = None




class EngineInstanceOut(BaseModel):
    id: int
    engine_code: str
    engine_label: str = ""
    display_name: str
    connection_type: str
    endpoint: str
    shared_secret_env: str
    secret_present: bool = False        # 只看变量名有没有值，永不回显值
    enabled: bool
    note: str
    # 下面三样是**现查**结果，不来自库里任何一列
    health_state: str = "unknown"
    health_detail: str = ""
    engine_model: str = ""
    created_at: str = ""
    updated_at: str = ""




class ModelPolicyIn(BaseModel):
    """保存策略入参。

    注：`capability` 与 `preference` 保持为 str 而不用 Literal，是因为
    save_policies 需要执行「一次说清所有问题」的整批汇总业务校验
    （见 test_策略校验一次说清所有问题），在路由层统一收集场景、能力、偏好等全部报错。
    """
    scene_key: str
    scene_label: str = ""
    capability: str
    required: list[str] = []
    slot_provider: str = ""
    slot_model: str = ""
    preference: str = "any"
    fallback_provider: str = ""
    fallback_model: str = ""
    resource_limit: dict = {}
    cloud_confirm_required: bool = False
    enabled: bool = False




# ==================== 智能体中心（0041） ====================
# 只有入参需要模型：这一族的**返回值**是跨表聚合（run × step × job × review），
# 形状由 `agents.py` 的视图函数决定。给它套一层 Out 模型会造出第二份字段清单，
# 而每个新字段都得改两处——上一轮 `response_model` 静默吞掉未声明字段的事
# 就是这一类形状（《发布基线》里那条"必须走 HTTP 量"的教训）。
class AgentRunIn(BaseModel):
    """发起一次智能体运行。

    `episode_id` 可空：整季盘点、跨集比对是项目级任务，不该被逼着选一个集。
    `goal` 是人的原话——它会被冻进 run，事后要能回答"他要的是什么、
    平台理解成了什么"，所以这里不接受结构化模板，只要那句话。
    """
    project_id: int
    episode_id: int | None = None
    goal: str = Field(min_length=1, max_length=2000)
    # 任务场景**引用** model_policy 的键；空串＝不按场景，走文本能力默认轨。
    # 这里不复制场景的任何字段（能力、建议轨、预算都在策略表里）
    scene_key: str = Field(default="", max_length=60)
    idempotency_key: str | None = Field(default=None, max_length=120,
                                        pattern=r"^[A-Za-z0-9._:-]+$")
    # 与 JobSubmitIn 同一句话：云端要授权，勾了才走。
    # 名称也刻意保持一致——两处两个名字会让人以为是两件事
    user_confirmed_cloud: bool = False
    # 有效期：人批的是"接下来这段时间内允许执行这一批参数"，不是永久授权。
    # 上下界与 0041 的 CHECK 同一条，改这里必须一起改那边
    approval_valid_seconds: int = Field(default=86400, ge=60, le=604800)


# ---------- 本地运行时路径（0051） ----------

class RuntimePathItem(BaseModel):
    """一项运行时路径的三份值：环境里的、网页存的、当前实际生效的。

    三份都要给，是因为这个功能最容易被误解的地方就是"我改了为什么没生效"：
      * `env_value` 有值 → `locked=True`，页面改不动（环境变量优先）；
      * `saved_value` 与 `effective` 不一致 → worker 还没重启。
    少给任何一份，人就得自己去猜是哪一种。
    """
    env_name: str
    label: str
    hint: str
    env_value: str | None = None
    saved_value: str | None = None
    effective: str | None = None
    # env / web / default —— 值从哪来。页面按它决定要不要显示"被环境占住"
    source: str = "default"
    locked: bool = False
    # runtime 探测结果：ok / missing / unknown（与 adapters.health 同一套词）
    state: str = "unknown"
    detail: str = ""


class RuntimePathsOut(BaseModel):
    items: list[RuntimePathItem] = []
    # 保存之后置 True：worker 拿的是启动那一刻的环境快照，改完必须重启才生效
    worker_needs_restart: bool = False
    note: str = ""


class RuntimePathIn(BaseModel):
    """保存一项路径。空串＝清除覆盖（回到跟随环境变量或内置默认）。"""
    path: str = Field(default="", max_length=1000)


# ---------- 云端密钥的指纹与连通性（2026-11-11） ----------

class SecretItem(BaseModel):
    """一把云端密钥的**指纹**——能确认是哪一把，推不回原值。

    三个字段各有各的必要性，少一个就答不全"这台机器上配的是什么"：
      * `fingerprint` sha256 前 8 位：对着供应商后台比对；
      * `length` 长度：复制粘贴多一个换行／少几个字符时能一眼看出；
      * `preview` 只露公开家族标记（`sk-` 这类）+ 指纹，**不含值的任何随机段**。
    """
    env_name: str
    provider: str
    label: str
    present: bool = False
    length: int = 0
    fingerprint: str = ""
    preview: str = ""


class SecretsOut(BaseModel):
    items: list[SecretItem] = []
    note: str = ""


class SecretProbeOut(BaseModel):
    """点「测试连接」的结论。`state` 词表与 `cloud_probe` 一致：
    ok／no_key／no_endpoint／auth／insufficient_balance／rate_limit／…"""
    provider: str
    label: str
    state: str
    detail: str
    key_env: str = ""
    has_key: bool = False


# ---------- 密钥的密文存储（0052，2026-11-11） ----------

class SecretValueIn(BaseModel):
    """保存密钥的请求体。**明文只在这个入参里存在一次**，落库前就变密文。

    刻意不给默认值：空串在语义上是"清除"还是"存一个空密钥"必须由路由
    明确判掉（清除走 DELETE），这里不做兜底。
    """
    value: str


class SecretValueOut(BaseModel):
    """一把密钥的**显示态**：任何时候都不含明文。

    `source` 三档回答的是"当前生效的是哪一份"，这是排查的第一步：
      * `env`  —— 环境变量里有，库里的（若有）不生效；
      * `db`   —— 库里解出来用；
      * `none` —— 两处都没有。
    """
    env_name: str
    present: bool = False
    source: str = "none"          # env / db / none
    fingerprint: str = ""
    length: int = 0
    preview: str = ""
    updated_at: str | None = None
    updated_by: int | None = None
    note: str = ""


class SecretValuesOut(BaseModel):
    """`kek_ready` 是界面判断"能不能配"的唯一依据。

    没有 KEK 时整个功能不可用——不是"能存但存的是明文"，而是**明说不可用**，
    免得人以为配上了。
    """
    items: list[SecretValueOut] = []
    kek_ready: bool = False
    kek_env: str = ""
    note: str = ""
