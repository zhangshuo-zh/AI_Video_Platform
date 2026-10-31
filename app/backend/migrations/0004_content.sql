-- 内容三件套：创作单元、分镜、资产。
--
-- 分镜的正文提示词仍可后续迁到文件，这里先只存"一镜一行"的结构化数据，
-- 因为生成作业要的就是这几列。
CREATE TABLE IF NOT EXISTS episode (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,              -- 项目内序号，手工可控，不从 max(id) 推
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft', -- draft|active|done
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (project_id, seq)
);

CREATE TABLE IF NOT EXISTS shot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id    INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,           -- 集内镜号
    summary       TEXT NOT NULL,              -- 这一镜要发生什么
    prompt        TEXT NOT NULL DEFAULT '',   -- 生成用的提示词，可与 summary 不同
    duration      REAL NOT NULL DEFAULT 5,    -- 秒
    status        TEXT NOT NULL DEFAULT 'todo', -- todo|queued|done|failed
    asset_ids     TEXT NOT NULL DEFAULT '[]', -- 引用的资产 id，JSON 数组
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (episode_id, seq)
);

CREATE TABLE IF NOT EXISTS asset (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL CHECK (kind IN ('character','scene','prop')),
    name               TEXT NOT NULL,
    summary            TEXT NOT NULL DEFAULT '',
    consistency_prompt TEXT NOT NULL DEFAULT '',
    ref_images         TEXT NOT NULL DEFAULT '[]',   -- 定妆图路径，JSON 数组
    status             TEXT NOT NULL DEFAULT 'draft', -- draft|ready
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 同一个项目里，同类资产不许重名：重名要么是同一角色（该合并），要么是起错了
CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_name ON asset(project_id, kind, name);
CREATE INDEX IF NOT EXISTS idx_shot_episode ON shot(episode_id, seq);
CREATE INDEX IF NOT EXISTS idx_asset_project ON asset(project_id, kind, name);
