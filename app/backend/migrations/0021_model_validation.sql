-- 0021：模型验证记录（P0：把"配置就绪"和"真的能产出"分开）
--
-- 这张表存在的唯一理由：`provider_config` 是十行定死的配置表，`PUT /api/providers`
-- 是整批覆盖；把"上次检查时是好的"写进配置行，等于推翻 W06 定下的"健康每次现查"。
-- 同理也不能塞进 job：job 的每一行都是可执行体，会被 worker 领走，还会污染成本汇总。
--
-- 四类 provenance（kind）互不升级：
--   env_check      只证明解释器／脚本／权重／密钥变量在不在，不证明能跑
--   mock           只证明平台的请求—响应—落盘—记账这套管道通了
--   real_call      真的起过子进程或打过供应商，且产物过了校验
--   target_machine 在目标机上跑过；没跑过就查无记录，界面显示 UNKNOWN，不填推算值
--
-- 只 INSERT、不 UPDATE／DELETE：改了 provider 配置不能把历史洗掉，
-- 一次真调用的证据必须留在它当时的快照上。

CREATE TABLE model_validation (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    capability          TEXT NOT NULL CHECK (capability IN ('text','image','video','tts','music')),
    backend             TEXT NOT NULL CHECK (backend IN ('local','cloud')),
    provider            TEXT NOT NULL DEFAULT '',
    model               TEXT NOT NULL DEFAULT '',
    adapter_code        TEXT NOT NULL DEFAULT '',
    manifest_version    TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL CHECK (kind IN ('env_check','mock','real_call','target_machine')),
    state               TEXT NOT NULL CHECK (state IN ('ok','missing','unknown','unconfigured',
                                                       'unregistered','failed')),
    detail              TEXT NOT NULL DEFAULT '',
    checks_json         TEXT NOT NULL DEFAULT '{}',
    params_json         TEXT NOT NULL DEFAULT '{}',
    -- 以下三列只有真调用与目标机基准才有；env_check/mock 一律留 NULL，
    -- 这样"有没有花过钱"能从数据上直接看出来，不靠人记。
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

CREATE INDEX idx_validation_slot ON model_validation(capability, backend, provider, created_at DESC);
CREATE INDEX idx_validation_kind ON model_validation(kind, created_at DESC);
CREATE INDEX idx_validation_state ON model_validation(state, kind);
