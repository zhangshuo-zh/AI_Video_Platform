-- 0031：源版本（分镜改动留痕）
--
-- 为什么要这两列：成片给每镜取"最新成功"的视频，而分镜或引用资产改了之后，
-- 旧视频仍然成功、产物签字也仍然匹配它的清单——平台没有任何字段能回答
-- "这段视频是按哪版分镜生成的"。于是改完剧本继续用旧片成了默认行为。
--
-- `shot.revision` / `shot.updated_at` 让"这一镜改过"留下痕迹（写路径只有
-- repositories/content.shots.update 一处，在那里 +1，不引入触发器）；
-- `job.source_version` 让每条作业记住**提交那一刻**的源内容指纹。
-- 两者一比就知道产物是不是过期：过期不算已签，也不许拼进成片。
--
-- 指纹只覆盖源内容（分镜四字段 + 引用资产的版本信息），不覆盖模型与参数——
-- 换模型不改变"这一镜讲什么"，换模型的选择在 Job 的参数快照里另外记着。
ALTER TABLE shot ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
ALTER TABLE shot ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE job ADD COLUMN source_version TEXT NOT NULL DEFAULT '';

UPDATE shot SET updated_at = created_at WHERE updated_at = '';

-- 产线时间线要多记一种事件：生产前检查的提醒（`preflight_warning`）。
-- SQLite 改不了 CHECK 约束，只能重建表；事件表没有被任何表外键引用，重建是安全的。
CREATE TABLE pipeline_event_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    node_id      INTEGER REFERENCES pipeline_node_run(id) ON DELETE SET NULL,
    event_type   TEXT NOT NULL CHECK (event_type IN (
                     'run_created','run_started','run_paused','run_resumed',
                     'run_cancelled','run_completed','run_failed',
                     'node_ready','node_started','node_completed','node_failed',
                     'node_skipped','node_retried','node_waiting_review',
                     'node_attention','job_submitted','preflight_warning')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
INSERT INTO pipeline_event_new (id, run_id, node_id, event_type, payload_json, created_at)
    SELECT id, run_id, node_id, event_type, payload_json, created_at FROM pipeline_event;
DROP TABLE pipeline_event;
ALTER TABLE pipeline_event_new RENAME TO pipeline_event;
CREATE INDEX IF NOT EXISTS idx_pipeline_event_run ON pipeline_event(run_id, id);
