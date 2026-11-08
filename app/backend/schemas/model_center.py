from pydantic import BaseModel, Field


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
    capability: str
    backend: str
    provider: str
    model_code: str
    display_name: str = ""
    # 空串＝"跟着 backend 走"。默认写死 "local" 会让云端登记在仓库页显示成
    # 本地——界面只传 backend，source 是给人看的第二口径，不该和它打架。
    source: str = ""
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
    source: str | None = None
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
    connection_type: str = "local_http"
    shared_secret_env: str = ""
    enabled: bool = True
    note: str = ""




class EngineInstancePatch(BaseModel):
    """逐字段更新。`engine_code` 与 `endpoint` 是这一行的键，改不了——
    与登记弹窗那两格同一口径：改键＝换一台机器，应该新建一条再停旧的。"""
    display_name: str | None = None
    connection_type: str | None = None
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
