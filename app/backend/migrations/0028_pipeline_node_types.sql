-- 0028：产线节点类型补阶段 D 的三格
--
-- 0026 把 node_type 写成 CHECK 枚举，是为了不让前端传一个字符串就当执行函数名用。
-- 阶段 D 多了成片合成／成片验收／成片审核三类节点，旧 CHECK 会把它们挡在
-- 建运行那一刻（IntegrityError，不是 422）——所以必须重建这张表。
--
-- SQLite 改不了 CHECK，只能"建新表 → 拷数据 → 删旧表 → 改名 → 重建索引"。
-- 迁移器在 BEGIN 之前关外键、每个文件一个事务，下面这几步是原子的。
-- pipeline_event.node_id 的外键指向表名（不是物理 id），改名后仍然有效。

CREATE TABLE pipeline_node_run_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    node_key     TEXT NOT NULL,
    -- 稳定枚举：阶段 D 追加 export_compose / export_validate / export_review
    node_type    TEXT NOT NULL CHECK (node_type IN (
                     'project_input','episode_input','document_input',
                     'shot_validate','asset_validate','human_gate',
                     'image_generate','image_review','video_generate',
                     'artifact_validate','shot_review','audio_prepare',
                     'export_prepare','export_compose','export_validate',
                     'export_review')),
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                     'pending','running','waiting_review','attention',
                     'completed','failed','skipped','cancelled')),
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    label        TEXT NOT NULL DEFAULT '',
    input_json   TEXT NOT NULL DEFAULT '{}',
    output_json  TEXT NOT NULL DEFAULT '{}',
    job_id       INTEGER REFERENCES job(id) ON DELETE SET NULL,
    review_id    INTEGER REFERENCES review_item(id) ON DELETE SET NULL,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    error_code   TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at   TEXT,
    finished_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO pipeline_node_run_new
    (id, run_id, node_key, node_type, status, depends_on_json, label,
     input_json, output_json, job_id, review_id, retry_count,
     error_code, error_message, started_at, finished_at, created_at)
SELECT id, run_id, node_key, node_type, status, depends_on_json, label,
       input_json, output_json, job_id, review_id, retry_count,
       error_code, error_message, started_at, finished_at, created_at
  FROM pipeline_node_run;

DROP TABLE pipeline_node_run;

ALTER TABLE pipeline_node_run_new RENAME TO pipeline_node_run;

CREATE UNIQUE INDEX uq_pipeline_node
    ON pipeline_node_run(run_id, node_key);
CREATE INDEX idx_pipeline_node_run
    ON pipeline_node_run(run_id, id);
CREATE INDEX idx_pipeline_node_job
    ON pipeline_node_run(job_id);
