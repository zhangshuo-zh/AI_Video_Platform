-- 0039：删项目不许带走作业与成本账（P0 · 测试案例 PROJ-08 抓到）
--
-- 为什么动这张表：0015 把 `job.project_id` 写成 `ON DELETE CASCADE`，
-- 于是"删项目"会把这一项目花过的钱整段抹掉。而平台从 W08 起就把账当红线：
--   * 《系统使用说明书》§2.5「作业（Job）与已经落盘的产物**不会**被删：
--     它们是花钱的记录，留在生成中心里可查」；
--   * §3.2 删一集也是同一句「作业与产物留在项目里」；
--   * 删集那条路确实做到了——`episode_id`/`shot_id` 是 `ON DELETE SET NULL`。
-- 只有项目这一层漏了，而且漏在最贵的动作上：一次误删 = 成本账、日志、产物路径全没。
--
-- 改法照 `episode_id` 的既有口径来：`project_id` 去掉 NOT NULL，改成 `ON DELETE SET NULL`。
-- 作业行与它的日志/产物目录从此都活着，只是不再属于哪个项目。
--
-- 谁能看见这些"无主作业"：`access.ensure()` 对 `project_id IS NULL` 只放行管理员，
-- 别人一律 404（与"看不见就是不存在"同一条纪律）；`jobs.list()` 本来就是
-- "管理员＝project_ids=None＝不加项目过滤"，所以无主作业自然只出现在管理员的
-- 「全部项目」视图里。成本账（`cost_summary`）按可见项目聚合，无主作业不进任何
-- 项目的账——这是有意的：项目已经不在了，硬塞进谁的账都是假账。
--
-- SQLite 改不了列的约束，只能整表重建（0022/0025 同一套写法）：
-- 建新表 → 逐列点名搬数据 → 删旧表 → 改名 → 重建索引。
-- `migrate.py` 在迁移期间关掉外键，所以子表（pipeline_node.job_id、
-- composition.job_id、asset_variant.source_job_id、audio_clip.job_id、
-- model_validation.job_id）指向的名字 `job` 在改名后依旧成立。
-- id 是显式搬过来的，AUTOINCREMENT 序列会跟着抬到 MAX(id)，不会回头撞旧号。

CREATE TABLE job_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER REFERENCES project(id) ON DELETE SET NULL,
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
    updated_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resources_json       TEXT NOT NULL DEFAULT '{}',
    actual_cost_micros   INTEGER,
    reconciled_by        INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    reconciled_at        TEXT,
    reconcile_note       TEXT NOT NULL DEFAULT '',
    attention_reason     TEXT NOT NULL DEFAULT '',
    superseded_by_job_id INTEGER REFERENCES job(id) ON DELETE SET NULL,
    source_version       TEXT NOT NULL DEFAULT ''
);

-- 逐列点名：一列都不漏，也不给旧行造出新默认值。
INSERT INTO job_new
    (id, project_id, episode_id, shot_id, capability, kind, execution_mode,
     provider_code, model_code, adapter_version, prompt_snapshot, params_json,
     input_refs_json, status, priority, idempotency_key, retry_of_job_id, attempt,
     user_confirmed_cloud, queued_at, started_at, finished_at, elapsed_ms, progress,
     worker_id, lease_until, return_code, error_code, error_message, log_path,
     result_paths_json, provider_request_id, quantity, unit, unit_price_micros,
     cost_micros, currency, pricing_snapshot_json, billable_status, created_by,
     created_at, updated_at, resources_json, actual_cost_micros, reconciled_by,
     reconciled_at, reconcile_note, attention_reason, superseded_by_job_id,
     source_version)
SELECT id, project_id, episode_id, shot_id, capability, kind, execution_mode,
       provider_code, model_code, adapter_version, prompt_snapshot, params_json,
       input_refs_json, status, priority, idempotency_key, retry_of_job_id, attempt,
       user_confirmed_cloud, queued_at, started_at, finished_at, elapsed_ms, progress,
       worker_id, lease_until, return_code, error_code, error_message, log_path,
       result_paths_json, provider_request_id, quantity, unit, unit_price_micros,
       cost_micros, currency, pricing_snapshot_json, billable_status, created_by,
       created_at, updated_at, resources_json, actual_cost_micros, reconciled_by,
       reconciled_at, reconcile_note, attention_reason, superseded_by_job_id,
       source_version
FROM job;

DROP TABLE job;
ALTER TABLE job_new RENAME TO job;

-- 七个索引原样重建（0015 四个、0020 两个、0032 一个）。
-- 写法与原来一致，只是 idx_job_project 现在也要能命中 NULL：
-- 管理员的「全部项目」视图不带项目过滤，靠的是 idx_job_queue；
-- 这一条留给"按项目取最近作业"的老路径，语义没变。
CREATE INDEX IF NOT EXISTS idx_job_queue ON job(status, priority DESC, queued_at, id);
CREATE INDEX IF NOT EXISTS idx_job_project ON job(project_id, queued_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_shot ON job(shot_id);
CREATE INDEX IF NOT EXISTS idx_job_retry ON job(retry_of_job_id);
CREATE INDEX IF NOT EXISTS idx_job_superseded ON job(superseded_by_job_id);
CREATE INDEX IF NOT EXISTS idx_job_reconciled ON job(billable_status, reconciled_at);
CREATE INDEX IF NOT EXISTS idx_job_episode_status ON job(episode_id, status, shot_id);
