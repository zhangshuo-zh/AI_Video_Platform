-- 0045：远程自建也能留验证痕——model_validation 的 backend 扩到 remote
--
-- 背景（2026-11-07 设计审查）：登记层（0035）自 v0.99 起允许 `remote`——
-- 局域网里另一台机器跑的服务，显式标出来免得被显示成"云端"。但验证表
-- （0021／0022）的 CHECK 只认 local／cloud，于是这一档：
--   * `POST /api/models/{id}/check` 直接 422（"本轮不接验证"）；
--   * `model_center._registered_row()` 把它的 `kinds` **硬置成空集**
--     （不置空会拿本地同 provider 的记录错认成它的——那等于拿另一台机器的凭据
--     给这台远程服务背书）；
--   * 状态因此永远停在 `unverified`，每次都要向人解释"不是坏了"。
--
-- 现在的判断：**"验不了"和"不该验"是两件事**。remote 的服务是真的可以跑、
-- 真的可以被验证的（人连得上就能点环境检查、能跑一条作业）；当初不接是因为
-- 表装不下那个值，不是因为这件事没有意义。所以扩 CHECK，让它能留痕。
--
-- 只改 CHECK 的一个取值，列一列没动。SQLite 的 CHECK 不可 ALTER → 整表重建，
-- 步骤与 0022 相同（建新表 → 逐列搬 → 删旧表 → 改名 → 重建索引）。
-- 索引与 0043 一致：`idx_validation_slot_model` 带 model，并补回另两把。
--
-- 折回（`MANIFEST_AXIS`）仍然只管**查清单**：remote 那一档的参数与计价
-- 按 local 轴认，但验证记录仍然记在 remote 自己名下——两条轴不许混用。

CREATE TABLE model_validation_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    capability          TEXT NOT NULL CHECK (capability IN ('text','image','video','tts','music')),
    -- 0045：多一档 remote。它就是登记层 0035 的那个取值，不是新概念。
    backend             TEXT NOT NULL CHECK (backend IN ('local','cloud','remote')),
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

-- 逐列点名搬运，别用 INSERT SELECT *：将来有人调整列顺序时这一句仍然安全。
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

-- 四把索引照重建前的样子补齐（重建表会把索引一起带走）。
-- `idx_model_validation_job` 是 0036 给 job_id 这个 SET NULL 子列加的外键索引
-- （删作业时要顺着它找子行）——重建时漏掉一把，`test_挂作业的子表外键列都有索引`
-- 就会红，而且那是真退化：每次删 Job 都会全表扫一遍验证记录。
CREATE INDEX idx_validation_slot_model
    ON model_validation(capability, backend, provider, model, created_at DESC);
CREATE INDEX idx_validation_kind ON model_validation(kind, created_at DESC);
CREATE INDEX idx_validation_state ON model_validation(state, kind);
CREATE INDEX idx_model_validation_job ON model_validation(job_id);
