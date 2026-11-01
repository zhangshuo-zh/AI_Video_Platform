-- 0016：成本快照与参数模板（W08）
--
-- 成本这几列在 0015 就建好了，W08 才真正开始写。这一份迁移只补两样：
--
-- 1. `job.resources_json`：本地作业的机时证据。金额只对云端有意义，
--    本地作业记 0 元，但"跑了多久、占了多少 CPU、峰值内存多少"要留下来——
--    它是判断"产能溢出该不该转云端"的唯一依据。存 JSON 而不是三列，
--    因为以后可能补 GPU／显存；字段名在 worker 里生成，不在这里猜。
--
-- 2. `generation_profile`：参数模板。数据字典 §5.4 的表，project_id 为 NULL
--    表示全局模板（只有管理员能写）。模板只存"提交时预填什么"，
--    不参与执行；改模板不影响已经提交的 Job（Job 自己的 params_json 才是真相）。
--
-- 金额精度：单价与金额都以 micros（1e-6 货币单位）存整数，数量仍是 REAL。
-- 用整数是为了避开浮点累加误差；展示时再除 1e6。多币种按 currency 分账，
-- 绝不把 USD 加进 CNY 的合计里。

ALTER TABLE job ADD COLUMN resources_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS generation_profile (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER REFERENCES project(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    capability    TEXT NOT NULL
                  CHECK (capability IN ('text','image','video','tts','music')),
    provider_code TEXT NOT NULL DEFAULT '',
    model_code    TEXT NOT NULL DEFAULT '',
    params_json   TEXT NOT NULL DEFAULT '{}',
    is_default    INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_by    INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 同一范围、同一能力下模板名不重复。SQLite 的 UNIQUE 把 NULL 当互不相等，
-- 全局模板（project_id IS NULL）这条索引管不到，得在仓储层再拦一次。
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_name
    ON generation_profile(project_id, capability, name);
CREATE INDEX IF NOT EXISTS idx_profile_scope
    ON generation_profile(project_id, capability, is_default);
