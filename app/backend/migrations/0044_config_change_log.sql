-- 0044：选路配置的最小变更痕——"默认轨是谁、在什么时候指到哪一档"
--
-- 背景（2026-11-07 设计审查 P0-3/P2-2）：`provider_config.is_default` 是选路的
-- 唯一来源（`resolve_provider()` 只认它），但这一行只有 `updated_at`、没有
-- `updated_by`，`replace_many()` 整批覆盖，`model_registration` 的撤登记直接删行。
-- 于是"默认轨什么时候被谁指到哪一档"没人答得上来——验证表（model_validation）
-- 给每个档留了痕，**触发变更的动作**反而没留。审计整体不做是 v0.87 的裁定
-- （设计 §16 第 14 条），不推翻；这里只补**选路**这一件事：一条 append-only
-- 的窄表，三个写点各记一行，不做通用审计、不回填历史。
--
-- 三个写点（routers 层）：
--   * PUT /api/providers        —— 能力轨整批保存（含默认轨变更）
--   * DELETE /api/models/{id}   —— 撤登记（默认轨可能正指着这一档）
--   * PUT /api/model-policies   —— 策略整批保存（建议轨/备用轨是提示的来源）
--
-- 形状抄 engine_probe_log（0042）的纪律：只追加、不改不删；subject 记"什么东西
-- 变了"，payload 记"从什么变成什么"，人快照进 created_by。不外键——撤登记后
-- 这一行的 subject 已经不存在，行本身还要留着回答"当时是谁撤的"。

CREATE TABLE IF NOT EXISTS config_change_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- subject 取值：'provider_slot'（能力轨一行）／'model_registration'（一条登记）
    -- ／'model_policy'（一个场景的策略）。用 TEXT 不用外键：
    -- 撤销类动作之后主体没了，这一行还得 standalone 地站着。
    subject       TEXT NOT NULL CHECK (subject IN
                     ('provider_slot', 'model_registration', 'model_policy')),
    -- 人读的主键串：provider_slot 用 "video/local"，model_registration 用
    -- "video/local/fastmetal/fastmetal-1.3b-qad"，model_policy 用场景键。
    -- 查的时候不用 join 就知道说的是哪一行。
    subject_key   TEXT NOT NULL,
    action        TEXT NOT NULL CHECK (action IN ('update', 'delete')),
    summary       TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    created_by    INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 这张表只有两种读法："最近改了什么"（倒序一页）与"这一样东西的历史"
-- （按 subject_key 前缀）。一个倒序索引两样都答得了。
CREATE INDEX IF NOT EXISTS idx_config_change_recent
    ON config_change_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_change_subject
    ON config_change_log(subject, subject_key, id DESC);
