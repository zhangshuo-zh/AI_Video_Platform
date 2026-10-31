-- 项目不再分类型：一个项目就是一条"小说 → 剧本 → 视频"的流水线。
--
-- SQLite 改表只能重建。重建期间要关外键（见 migrate.py），
-- 否则 episode / asset 指向 project 的外键会让 DROP 失败。
CREATE TABLE project_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

INSERT INTO project_new (id, name, status, created_at)
SELECT id, name, status, created_at FROM project;

DROP TABLE project;
ALTER TABLE project_new RENAME TO project;
