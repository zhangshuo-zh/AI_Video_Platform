-- 项目列表要显示"修改时间"。
--
-- 为什么不 ALTER TABLE ADD COLUMN：SQLite 不许给新列带表达式默认值
-- （实测报 "Cannot add a column with non-constant default"），
-- 而这一列的默认值就是 datetime('now','localtime')。所以沿用 0005 的重建写法，
-- 重建期间的外键由 migrate.py 统一关掉、跑完再 foreign_key_check。
CREATE TABLE project_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 存量行的"修改时间"先按创建时间填：没有别的凭据可查，
-- 而且新项目两个时间相同本来就该如此。
INSERT INTO project_new (id, name, status, created_at, updated_at)
SELECT id, name, status, created_at, created_at FROM project;

DROP TABLE project;
ALTER TABLE project_new RENAME TO project;
