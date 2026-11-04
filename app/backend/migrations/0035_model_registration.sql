-- 0035：模型中心——模型登记（实例层）与任务调用策略（配置层）
--
-- 为什么需要这两张表，而 `provider_config` 与 `adapters.py` 都不够：
--
-- 1) `adapters.py` 是**规格**：参数范围、倍数、互斥、计价、出处、哪天核过。
--    `validate_params()` 与 `health()` 都直接读它。把它搬进库表等于把安全边界
--    交给页面输入框，而且"出处"这种事实是人去官方文档核回来的，填表填不出来。
--    ⇒ 规格继续住代码里，页面只读。
-- 2) `provider_config` 是**选型**：能力 × 后端 十行定死，一行只装得下"这一类走哪条轨"，
--    装不下"这台机器上这一档模型在哪个路径、哪一版、启没启用"。
--    一个 provider 下面有多档模型（fastmetal 有 1.3B 和 5B），十行根本表达不了。
--    ⇒ 实例登记另开一张表，按 (capability, backend, provider, model_code) 指向清单里那一档，
--      只存"这台机器上的事实"，不复制规格。
-- 3) 任务调用策略：`generation_profile` 已经有 provider_code/model_code/params，
--    但它是**参数模板**（一次提交要带哪些参数），没有"任务场景 → 需要哪些能力"这一层，
--    而且执行侧零消费。把场景塞进 profile 会让一个实体有两个东家。
--    ⇒ 策略单独一张表；`generation_profile` 原样不动。
--
-- 裁定 MC1（2026-11-04）＝只提示不自动切：本表**不参与** `resolve_provider()` 的选路，
-- 默认轨的唯一来源仍是 `provider_config.is_default`。策略页保存只改配置，
-- 不启动任务、不换轨、不兜底——这条写在表注释里，是为了下一个改代码的人
-- 不会顺手在这里接上 fallback。

CREATE TABLE model_registration (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    capability      TEXT NOT NULL CHECK (capability IN ('text','image','video','tts','music')),
    backend         TEXT NOT NULL CHECK (backend IN ('local','cloud','remote')),
    -- provider 与 model_code 是**指向清单的键**，不是清单的副本：
    -- 清单里没有这一档时这里照样能存（界面必须显示"清单未登记，参数不猜"），
    -- 但绝不能因此就允许提交——提交侧走的还是 adapters.find()，找不到就拒。
    provider        TEXT NOT NULL CHECK (length(trim(provider)) > 0),
    model_code      TEXT NOT NULL CHECK (length(trim(model_code)) > 0),
    display_name    TEXT NOT NULL DEFAULT '',
    -- 来源与"后端"是一对两个概念：backend 是平台执行侧的分流（本地子进程 / 云端 HTTP /
    -- 远程自建服务），source 是人话口径的运行位置。默认跟着 backend 走，
    -- 但允许 remote 这一类显式标出来，免得"局域网里另一台机器"被显示成"云端"。
    source          TEXT NOT NULL DEFAULT 'local'
                        CHECK (source IN ('local','cloud','remote')),
    engine_code     TEXT NOT NULL DEFAULT '',       -- 所属引擎（＝清单里的 adapter code）
    version         TEXT NOT NULL DEFAULT '',
    artifact_path   TEXT NOT NULL DEFAULT '',       -- 本地：权重目录或文件；不校验存在性，
                                                    -- 存在性由 runtime.py 现查，不落库
    remote_model_id TEXT NOT NULL DEFAULT '',       -- 云端／远程：供应商那边那个模型 ID
    -- 人工声明的能力标签。只有两种合法来源，界面分开显示：
    --   清单派生（image_inputs / params / prices）——可复算，不存这张表
    --   人工声明（本列）——未核验时界面必须带"人工声明 · 未核验"
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    enabled         INTEGER NOT NULL DEFAULT 1,
    note            TEXT NOT NULL DEFAULT '',
    created_by      INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    -- 同一档模型在同一轨上只能登记一次。要登记"同一档的两个本地版本"，
    -- 走 version 列而不是再来一行——多行会让"这一档现在用哪个"变成猜。
    UNIQUE (capability, backend, provider, model_code)
);

CREATE INDEX idx_reg_capability ON model_registration(capability, backend, enabled);
CREATE INDEX idx_reg_engine     ON model_registration(engine_code);

CREATE TABLE model_policy (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 任务场景：人物履历／提示词／定妆图／服装变体／场景概念图／道具概念图／
    -- 文本提取／分镜／镜头视频。键定死在代码里（services.POLICY_SCENES），
    -- 表里只存"这个场景配成什么样"，不发明新场景——
    -- 场景名一旦能自由输入，就会长出十个同义词指向同一件事。
    scene_key       TEXT NOT NULL UNIQUE,
    scene_label     TEXT NOT NULL DEFAULT '',
    capability      TEXT NOT NULL CHECK (capability IN ('text','image','video','tts','music')),
    -- 必需能力：界面按它筛候选，筛的依据是清单派生标签 + 已验证的人工声明。
    required_json   TEXT NOT NULL DEFAULT '[]',
    -- 建议轨（不是默认轨！默认轨唯一来源是 provider_config.is_default）
    slot_provider   TEXT NOT NULL DEFAULT '',
    slot_model      TEXT NOT NULL DEFAULT '',
    preference      TEXT NOT NULL DEFAULT 'any'
                        CHECK (preference IN ('local','cloud','remote','any')),
    -- 备用轨：MC1 裁甲 ⇒ 只作为"可改选"的提示出现，提交仍需人工显式选择
    fallback_provider TEXT NOT NULL DEFAULT '',
    fallback_model    TEXT NOT NULL DEFAULT '',
    resource_limit_json TEXT NOT NULL DEFAULT '{}',
    -- 云端授权与费用保护：这一档为 1 时，界面上的候选必须带"要逐次确认计费"，
    -- 真正的闸门仍在 submit_job 的 user_confirmed_cloud，这里不重复判
    cloud_confirm_required INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 0,     -- 0＝还没配好，界面显示"待配置"
    updated_by      INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX idx_policy_capability ON model_policy(capability, enabled);

-- 种子一：把现役库里已经填了的槽位落成登记行，避免模型中心上线第一天仓库页是空的。
-- 只登记 provider_config 里真填了的两列，没填的槽位不造行——
-- 造一行"空登记"等于凭空多出一个不存在的模型。
INSERT OR IGNORE INTO model_registration
        (capability, backend, provider, model_code, display_name, source, engine_code,
         note, enabled, created_by)
SELECT  pc.capability,
        pc.backend,
        pc.provider,
        pc.model,
        '',
        -- 老数据只有 local/cloud 两档，直接照搬；remote 是本轮新加的，历史行不可能有
        pc.backend,
        pc.provider,
        '由 0035 从 provider_config 现有配置自动登记（2026-11-04）',
        pc.enabled,
        NULL
  FROM provider_config pc
 WHERE trim(pc.provider) <> '' AND trim(pc.model) <> '';

-- 种子二：九个任务场景先立骨架，全部 enabled=0（待配置）。
-- 骨架落库而不是前端写死，是为了让"场景有几个"这件事只有一个答案。
INSERT OR IGNORE INTO model_policy (scene_key, scene_label, capability, required_json) VALUES
    ('character_bio',      '人物履历与性格生成',   'text',  '["文本生成","结构化输出"]'),
    ('scene_prompt',       '场景说明与提示词生成', 'text',  '["文本生成"]'),
    ('character_base',     '人物基础形象生成',     'image', '["文生图"]'),
    ('character_variant',  '人物服装形态生成',     'image', '["图生图","参考图"]'),
    ('scene_concept',      '场景概念图生成',       'image', '["文生图"]'),
    ('prop_concept',       '道具概念图生成',       'image', '["文生图"]'),
    ('text_extraction',    '文本提取与结构化整理', 'text',  '["结构化输出"]'),
    ('storyboard',         '分镜生成',             'text',  '["文本生成","结构化输出"]'),
    ('shot_video',         '镜头视频生成',         'video', '["文生视频"]');
