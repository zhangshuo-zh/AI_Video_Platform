-- 0027：成片中心（阶段 D）
--
-- 成片不是第二个队列：每一次合成都是一条普通 `job`（provider=ffmpeg），
-- 由同一个 worker 领取、同一个日志与产物目录承载。这张表只记住
-- "这一次合成用的是哪一版时间线、哪条 Job、产出了哪个文件"。
--
-- 三条硬约束写进表结构：
--   * `(episode_id, version)` 唯一——同一集每次合成版本加一，旧版本不覆盖；
--   * 时间线与参数冻结成快照（`timeline_json`／`params_json`），
--     事后改分镜或换素材不会改写"当时合的是什么"；
--   * **不存状态列**。状态从关联 Job 现算：Job 被删（SET NULL）时界面显示
--     "关联作业已不在"，而不是凭空变成草稿——两套状态互相同步正是 v0.86
--     修过的那类 bug。
--
-- `output_path` 与 `sha256` 只记最终产物；中间文件与 ffprobe 回执留在
-- Job 自己的 outputs/ 下，不搬进库。

CREATE TABLE IF NOT EXISTS composition (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    episode_id      INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    -- 从产线发起时指向那条运行；手工发起为 NULL
    pipeline_run_id INTEGER REFERENCES pipeline_run(id) ON DELETE SET NULL,
    version         INTEGER NOT NULL,
    -- 冻结的时间线：镜头顺序、时长、视频产物引用、审核结论、音频引用、字幕
    timeline_json   TEXT NOT NULL,
    -- 合成参数：分辨率、帧率、编码、是否混音
    params_json     TEXT NOT NULL DEFAULT '{}',
    job_id          INTEGER REFERENCES job(id) ON DELETE SET NULL,
    output_path     TEXT,                 -- 相对 JOBS_DIR；不存绝对路径
    sha256          TEXT,
    bytes           INTEGER,
    duration_ms     INTEGER,
    width           INTEGER,
    height          INTEGER,
    created_by      INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_composition_version
    ON composition(episode_id, version);
CREATE INDEX IF NOT EXISTS idx_composition_project
    ON composition(project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_composition_job
    ON composition(job_id);
