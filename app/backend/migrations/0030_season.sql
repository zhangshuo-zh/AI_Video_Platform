-- 0030：整季生产（阶段 E-2）
--
-- 一季＝一组集的一次批量排产。它不是第二个队列，也不复制单集的节点：
-- 每一集仍然是一条 `pipeline_run`（及其 Job），season_item 只记住"这一季包含了哪几集、
-- 对应哪条运行"。这样单集的暂停／重试／签字语义原样复用，不需要两套调度。
--
-- 两条硬约束：
--   * `(season_id, episode_id)` 唯一：一季里同一集只排一次；
--   * 四季状态里只存"人下的命令"（paused／cancelled／finished_at），
--     "进行中／已完成／有失败"由 items 与运行现算，避免第二套状态真相。
--
-- `max_active_runs` 是这一季同时推进几集的上限，建季时冻结；改上限要新建一季。
-- `idempotency_key` 让"开始整季"重复点不会排两季。

CREATE TABLE IF NOT EXISTS season_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    template_id     INTEGER NOT NULL REFERENCES pipeline_template(id)
                        ON DELETE RESTRICT,
    name            TEXT NOT NULL DEFAULT '',
    max_active_runs INTEGER NOT NULL DEFAULT 1 CHECK (max_active_runs >= 1),
    idempotency_key TEXT UNIQUE,
    paused          INTEGER NOT NULL DEFAULT 0,
    cancelled       INTEGER NOT NULL DEFAULT 0,
    cancel_reason   TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at     TEXT,
    created_by      INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_season_run_project
    ON season_run(project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_season_run_active
    ON season_run(cancelled, paused, finished_at, id);

CREATE TABLE IF NOT EXISTS season_item (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES season_run(id) ON DELETE CASCADE,
    episode_id      INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    -- 对应哪条单集运行。NULL＝还没开始，或因输入不成立被拦下
    pipeline_run_id INTEGER REFERENCES pipeline_run(id) ON DELETE SET NULL,
    position        INTEGER NOT NULL,
    -- 启动失败的人话（没有镜头、模板不成立、同一集已有别的运行）。
    -- 只是本季看到的失败原因；不作为运行状态的第二份真相。
    error_message   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(season_id, episode_id)
);

CREATE INDEX IF NOT EXISTS idx_season_item_season
    ON season_item(season_id, position, id);
CREATE INDEX IF NOT EXISTS idx_season_item_run
    ON season_item(pipeline_run_id);
