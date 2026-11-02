-- 0022：验证档位增加「产物验收」（artifact_review）
--
-- 评审 §3.1 把验证类型定成五档：环境检查、mock、真实调用、目标机基准、产物验收。
-- 前四档 0021 已有；这一档补的是最后一环——真实调用成功只证明"链路产出过文件"，
-- 文件本身好不好（画面对不对、口型对不对、有没有花屏）必须由人看过才算数。
--
-- SQLite 的 CHECK 约束没法 ALTER，只能整表重建：
-- 建新表（列顺序与 0021 完全一致）→ 搬数据 → 删旧表 → 改名 → 重建三把索引。
-- migrate.py 已经在迁移期间关掉外键，重建期间引用不受影响。
-- 历史照旧只 INSERT 不 UPDATE：旧记录一行不丢，新档位只追加。
--
-- 产物验收（artifact_review）的口径：
--   来源只能是「已成功的真实作业」（假适配器的成功不算，和 real_call 同一条规矩）；
--   state 记人的结论——通过＝'ok'，不通过＝'failed'，不是"没验过"；
--   生产可用从「环境齐＋真实调用过」升级为「环境齐＋真实调用过＋产物验收通过」，
--   三档都由服务端判定，前端只展示。

CREATE TABLE model_validation_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    capability          TEXT NOT NULL CHECK (capability IN ('text','image','video','tts','music')),
    backend             TEXT NOT NULL CHECK (backend IN ('local','cloud')),
    provider            TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    adapter_code        TEXT NOT NULL DEFAULT '',
    manifest_version    TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL CHECK (kind IN ('env_check','mock','real_call','target_machine',
                                                       'artifact_review')),
    state               TEXT NOT NULL CHECK (state IN ('ok','missing','unknown','unconfigured',
                                                       'unregistered','failed')),
    detail              TEXT NOT NULL DEFAULT '',
    checks_json         TEXT NOT NULL DEFAULT '{}',
    params_json         TEXT NOT NULL DEFAULT '{}',
    -- 以下各列只有真调用／目标机基准／产物验收才有；env_check/mock 一律留 NULL。
    job_id              INTEGER REFERENCES job(id) ON DELETE SET NULL,
    provider_request_id TEXT,
    http_status         INTEGER,
    latency_ms          INTEGER,
    artifact_path       TEXT,
    artifact_sha256     TEXT,
    cost_micros         INTEGER,
    currency            TEXT,
    paid_confirmed      INTEGER NOT NULL DEFAULT 0,
    created_by          INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 逐列点名搬运，别用 INSERT SELECT *：将来有人调整列顺序时，这一句仍然安全。
INSERT INTO model_validation_new
    (id, capability, backend, provider, model, adapter_code, manifest_version,
     kind, state, detail, checks_json, params_json, job_id, provider_request_id,
     http_status, latency_ms, artifact_path, artifact_sha256, cost_micros,
     currency, paid_confirmed, created_by, created_at)
SELECT
    id, capability, backend, provider, model, adapter_code, manifest_version,
    kind, state, detail, checks_json, params_json, job_id, provider_request_id,
    http_status, latency_ms, artifact_path, artifact_sha256, cost_micros,
    currency, paid_confirmed, created_by, created_at
FROM model_validation;

DROP TABLE model_validation;
ALTER TABLE model_validation_new RENAME TO model_validation;

CREATE INDEX idx_validation_slot ON model_validation(capability, backend, provider, created_at DESC);
CREATE INDEX idx_validation_kind ON model_validation(kind, created_at DESC);
CREATE INDEX idx_validation_state ON model_validation(state, kind);
