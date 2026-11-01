-- 0015：Job 表与 worker 心跳（W07）
--
-- 队列只由这一张表驱动，不另建"任务队列"。业务 Task 表暂不建：
-- 现在没有任何调用方需要"用户可跟踪的工作项"，W14 做审核时再从 Job/Review
-- 派生，避免先摆一张没人写、没人读的空表（规格 02 §7.1 明确说队列只由 Job 驱动）。
--
-- 三条硬约束写进表结构：
--   * status 只有八个值，不允许并存第二套状态机；
--   * idempotency_key 全局唯一（SQLite 的 UNIQUE 把 NULL 当互不相等，允许不传）；
--   * 金额用"最小货币单位"（micros，1e-6），quantity 用 REAL——数量是秒/帧/Token，
--     不是钱，不能为了整齐把它也塞成整数。
CREATE TABLE IF NOT EXISTS job (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    episode_id           INTEGER REFERENCES episode(id) ON DELETE SET NULL,
    shot_id              INTEGER REFERENCES shot(id) ON DELETE SET NULL,
    capability           TEXT NOT NULL
                         CHECK (capability IN ('text','image','video','tts','music')),
    kind                 TEXT NOT NULL
                         CHECK (kind IN ('text','image','video_local','video_cloud','tts','music')),
    execution_mode       TEXT NOT NULL CHECK (execution_mode IN ('local','cloud')),
    provider_code        TEXT NOT NULL,
    model_code           TEXT NOT NULL,
    adapter_version      TEXT NOT NULL DEFAULT '',
    prompt_snapshot      TEXT NOT NULL DEFAULT '',
    params_json          TEXT NOT NULL DEFAULT '{}',
    input_refs_json      TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued','starting','running','succeeded','failed',
                                           'cancel_requested','cancelled','needs_attention')),
    priority             INTEGER NOT NULL DEFAULT 0,
    idempotency_key      TEXT UNIQUE,
    retry_of_job_id      INTEGER REFERENCES job(id) ON DELETE SET NULL,
    attempt              INTEGER NOT NULL DEFAULT 1,
    user_confirmed_cloud INTEGER NOT NULL DEFAULT 0,
    queued_at            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at           TEXT,
    finished_at          TEXT,
    elapsed_ms           INTEGER,
    progress             REAL,
    worker_id            TEXT,
    lease_until          TEXT,
    return_code          INTEGER,
    error_code           TEXT,
    error_message        TEXT,
    log_path             TEXT,
    result_paths_json    TEXT NOT NULL DEFAULT '[]',
    provider_request_id  TEXT,
    quantity             REAL,
    unit                 TEXT,
    unit_price_micros    INTEGER,
    cost_micros          INTEGER,
    currency             TEXT,
    pricing_snapshot_json TEXT,
    billable_status      TEXT NOT NULL DEFAULT 'not_billable'
                         CHECK (billable_status IN ('known','pending_reconciliation','not_billable')),
    created_by           INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 领取按"优先级高 → 先来"排；把 id 也带上，同秒入队时顺序才是稳定的
CREATE INDEX IF NOT EXISTS idx_job_queue ON job(status, priority DESC, queued_at, id);
CREATE INDEX IF NOT EXISTS idx_job_project ON job(project_id, queued_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_shot ON job(shot_id);
CREATE INDEX IF NOT EXISTS idx_job_retry ON job(retry_of_job_id);

-- worker 心跳只有一行。在线判定看 heartbeat_at 的年龄，不靠进程号——
-- 重启后 PID 可能被复用，年龄才是不会骗人的那个。
CREATE TABLE IF NOT EXISTS worker_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    worker_id      TEXT NOT NULL DEFAULT '',
    pid            INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL DEFAULT '',
    heartbeat_at   TEXT NOT NULL DEFAULT '',
    current_job_id INTEGER,
    version        TEXT NOT NULL DEFAULT ''
);
