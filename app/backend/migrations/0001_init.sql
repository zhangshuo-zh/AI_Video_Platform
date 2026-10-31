-- 初始结构：项目表。
-- 这是 schema.sql 时代那张表，原样搬进迁移体系，老库通过"认领"跳过这一步。
CREATE TABLE IF NOT EXISTS project (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('drama', 'anime', 'volume')),
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
