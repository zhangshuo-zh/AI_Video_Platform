-- 扩展现有配置变更痕的白名单，现值仍归 worker_limit / daemon.json。
-- 不新增 Job、调度或监控状态表；保留旧变更记录、外键与两把索引。
CREATE TEMP TABLE config_log_sequence AS SELECT seq FROM sqlite_sequence WHERE name='config_change_log';
CREATE TABLE config_change_log_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- subject 取值：'provider_slot'（能力轨一行）／'model_registration'（一条登记）
    -- ／'model_policy'（一个场景的策略）。用 TEXT 不用外键：
    -- 撤销类动作之后主体没了，这一行还得 standalone 地站着。
    subject       TEXT NOT NULL CHECK (subject IN
                     ('provider_slot', 'model_registration', 'model_policy', 'worker_limit', 'worker_daemon')),
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

INSERT INTO config_change_log_new (id,subject,subject_key,action,summary,payload_json,created_by,created_at) SELECT id,subject,subject_key,action,summary,payload_json,created_by,created_at FROM config_change_log;
DROP TABLE config_change_log;
ALTER TABLE config_change_log_new RENAME TO config_change_log;
UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM config_log_sequence),0)) WHERE name='config_change_log';
DROP TABLE config_log_sequence;
-- 这张表只有两种读法："最近改了什么"（倒序一页）与"这一样东西的历史"
-- （按 subject_key 前缀）。一个倒序索引两样都答得了。
CREATE INDEX IF NOT EXISTS idx_config_change_recent
    ON config_change_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_config_change_subject
    ON config_change_log(subject, subject_key, id DESC);
