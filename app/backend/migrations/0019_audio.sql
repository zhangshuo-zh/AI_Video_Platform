-- 0019：音频素材（W15）
--
-- 音频中心只组织素材，不建第二条队列：每条素材要生成时，仍然落一条 job，
-- 由同一个 worker 跑。这里存的是"这条台词/音乐属于哪一集哪一镜"，
-- 以及作业成功后抄回来的产物路径。
--
-- viseme 不单独建表：它是某一条台词的后处理结果，跟着 clip 走；
-- 只有需要口型特写的镜头才填，普通旁白留空。
CREATE TABLE IF NOT EXISTS audio_clip (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    episode_id     INTEGER REFERENCES episode(id) ON DELETE SET NULL,
    shot_id        INTEGER REFERENCES shot(id) ON DELETE SET NULL,
    kind           TEXT NOT NULL
                   CHECK (kind IN ('dialogue','narration','music','sfx')),
    speaker        TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','queued','ready','failed')),
    path           TEXT,
    duration_ms    INTEGER,
    timestamps_json TEXT NOT NULL DEFAULT '[]',
    viseme_json    TEXT NOT NULL DEFAULT '[]',
    job_id         INTEGER REFERENCES job(id) ON DELETE SET NULL,
    notes          TEXT NOT NULL DEFAULT '',
    created_by     INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_audio_project
    ON audio_clip(project_id, kind, id DESC);
CREATE INDEX IF NOT EXISTS idx_audio_shot
    ON audio_clip(shot_id);
