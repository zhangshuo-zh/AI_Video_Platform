-- 项目归属与协作人。
--
-- project 要加 owner_id，得重建整表：SQLite 的 ADD COLUMN 不允许 NOT NULL
-- 又带非恒定默认值，而这里根本给不出恒定默认值——每行该是它自己的创建人。
-- 老数据没有这个信息，先回填再收紧。沿用 0005／0007 的重建写法，
-- 迁移期间的外键由 migrate.py 统一关掉、跑完再 foreign_key_check。
CREATE TABLE project_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    description TEXT NOT NULL DEFAULT '',
    -- RESTRICT：账号名下还有项目时，库这一层就删不掉人。
    -- 应用层必须先转移或连带删，别让"人没了项目悬空"这种状态出现。
    owner_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 存量项目没有创建人记录，全部记到超级用户名下；
-- 之后可以在界面上用「转移」改给别人。
INSERT INTO project_new (id, name, status, description, owner_id, created_at, updated_at)
SELECT p.id, p.name, p.status, p.description,
       COALESCE((SELECT id FROM app_user WHERE is_superuser = 1 ORDER BY id LIMIT 1),
                (SELECT id FROM app_user ORDER BY id LIMIT 1)),
       p.created_at, p.updated_at
  FROM project p;

DROP TABLE project;
ALTER TABLE project_new RENAME TO project;

-- 协作人＝可改可查，不含删项目、不含管成员——那两样只归所有人（与管理员）。
-- 两条 CASCADE：删项目或删账号时关系跟着走，不留孤儿行。
CREATE TABLE project_member (
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    added_by   INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_project_member_user ON project_member(user_id);
