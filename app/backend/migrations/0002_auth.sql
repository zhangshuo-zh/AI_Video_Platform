-- 身份与权限：用户、角色、权限、两层关联、会话。
--
-- 表名用 app_user 而不是 user：user 在 Postgres 里是保留字，
-- 将来换库时能少一处引号麻烦。
CREATE TABLE IF NOT EXISTS app_user (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE,
    display_name         TEXT NOT NULL,
    password_hash        TEXT NOT NULL,              -- scrypt 自描述串，见 security.py
    is_superuser         INTEGER NOT NULL DEFAULT 0, -- 1 = 绕过权限判定；新角色出现也自动拥有
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0, -- 初始密码登录后强制改
    created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_login_at        TEXT
);

CREATE TABLE IF NOT EXISTS role (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS permission (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,     -- 形如 project:write
    name       TEXT NOT NULL,
    group_name TEXT NOT NULL             -- 按模块分组，前端好排
);

CREATE TABLE IF NOT EXISTS user_role (
    user_id INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES role(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE IF NOT EXISTS role_permission (
    role_id       INTEGER NOT NULL REFERENCES role(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permission(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

-- 会话落库而不是签名 cookie：这样能单点吊销（踢人、改密码后全端下线）
CREATE TABLE IF NOT EXISTS session (
    token_hash   TEXT PRIMARY KEY,       -- sha256(随机 token)，库里不存明文
    user_id      INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_user ON session(user_id);

-- ---------- 权限点 ----------
INSERT OR IGNORE INTO permission (code, name, group_name) VALUES
    ('project:read',   '查看项目',   '项目'),
    ('project:write',  '新建与修改项目', '项目'),
    ('project:delete', '删除项目',   '项目'),
    ('asset:read',     '查看资产',   '资产'),
    ('asset:write',    '登记与修改资产', '资产'),
    ('doc:read',       '查看文档',   '创作'),
    ('doc:write',      '编辑文档',   '创作'),
    ('job:read',       '查看队列与产物', '生成'),
    ('job:submit',     '提交生成作业', '生成'),
    ('job:cancel',     '取消作业',   '生成'),
    ('review:read',    '查看审核',   '审核'),
    ('review:sign',    '审核签字',   '审核'),
    ('audio:read',     '查看音频任务', '音频'),
    ('audio:write',    '新建音频任务', '音频'),
    ('setting:read',   '查看设定',   '设定'),
    ('setting:write',  '编辑设定',   '设定'),
    ('user:read',      '查看用户',   '用户'),
    ('user:write',     '新建与修改用户', '用户'),
    ('user:assign',    '分配角色',   '用户'),
    ('system:config',  '系统设置',   '系统');

-- ---------- 角色 ----------
INSERT OR IGNORE INTO role (code, name, description) VALUES
    ('admin',  '管理员', '全部权限；超级用户绕过判定，新加的权限点自动生效'),
    ('editor', '制作',   '内容侧全流程，不含用户与系统设置'),
    ('viewer', '只读',   '只能看，不能改');

-- 管理员：拿全部权限点（用子查询，将来加权限点自动包含）
INSERT OR IGNORE INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE code = 'admin'), id FROM permission;

-- 制作：内容与生成相关
INSERT OR IGNORE INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE code = 'editor'), id FROM permission
WHERE code IN (
    'project:read', 'project:write',
    'asset:read', 'asset:write',
    'doc:read', 'doc:write',
    'job:read', 'job:submit', 'job:cancel',
    'review:read', 'review:sign',
    'audio:read', 'audio:write',
    'setting:read', 'setting:write'
);

-- 只读：所有 :read
INSERT OR IGNORE INTO role_permission (role_id, permission_id)
SELECT (SELECT id FROM role WHERE code = 'viewer'), id FROM permission
WHERE code LIKE '%:read';
