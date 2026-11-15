-- 0056：设定中心（画风词缀库 + 设定矛盾审阅）与产线视频对口型节点（lip_sync）

CREATE TABLE IF NOT EXISTS style_affix (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general'
                CHECK (category IN ('general','lighting','camera','quality','character')),
    positive    TEXT NOT NULL DEFAULT '',
    negative    TEXT NOT NULL DEFAULT '',
    is_default  INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_by  INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_style_affix_project
    ON style_affix(project_id, sort_order, id);

CREATE TABLE IF NOT EXISTS consistency_issue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    issue_key    TEXT NOT NULL,
    category     TEXT NOT NULL
                 CHECK (category IN ('character_conflict','asset_missing_ref','dialogue_speaker','prop_mismatch','continuity')),
    severity     TEXT NOT NULL DEFAULT 'warning'
                 CHECK (severity IN ('info','warning','error')),
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    source_type  TEXT NOT NULL DEFAULT ''
                 CHECK (source_type IN ('','shot','asset','document')),
    source_id    INTEGER,
    suggestion   TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','adopted','ignored')),
    resolved_by  INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    resolved_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_consistency_issue_key
    ON consistency_issue(project_id, issue_key);

CREATE INDEX IF NOT EXISTS idx_consistency_issue_project
    ON consistency_issue(project_id, status, id);

-- SQLite 无法直接 ALTER CHECK，沿用 0028 重建方式为 pipeline_node_run 加入 'lip_sync'
CREATE TABLE pipeline_node_run_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    node_key     TEXT NOT NULL,
    node_type    TEXT NOT NULL CHECK (node_type IN (
                     'project_input','episode_input','document_input',
                     'shot_validate','asset_validate','human_gate',
                     'image_generate','image_review','video_generate',
                     'artifact_validate','shot_review','audio_prepare',
                     'lip_sync',
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
