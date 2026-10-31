-- 模型双轨配置：每类能力固定两行（本地 / 云端）。
--
-- 为什么不是一张"模型表"：能力是固定的五类，后端只有本地和云端两种，
-- 组合起来就是十行。固定成十行，界面上永远对称，不会出现"某能力只有云端"的缺口。
-- 密钥不进这张表：只从环境变量读，库里只记 provider code。
CREATE TABLE IF NOT EXISTS provider_config (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    capability TEXT NOT NULL CHECK (capability IN ('text','image','video','tts','music')),
    backend    TEXT NOT NULL CHECK (backend IN ('local','cloud')),
    provider   TEXT NOT NULL DEFAULT '',      -- volc / minimax / ollama / mflux ...
    model      TEXT NOT NULL DEFAULT '',      -- 具体模型 ID，留空表示没配
    enabled    INTEGER NOT NULL DEFAULT 0,    -- 配齐了才置 1
    is_default INTEGER NOT NULL DEFAULT 0,    -- 提交作业时不指定后端就走这条
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (capability, backend)
);

-- 预置十行，默认都指向云端：本地那条要等新机器到货才可填
INSERT OR IGNORE INTO provider_config (capability, backend, is_default) VALUES
    ('text',  'local', 0), ('text',  'cloud', 1),
    ('image', 'local', 0), ('image', 'cloud', 1),
    ('video', 'local', 0), ('video', 'cloud', 1),
    ('tts',   'local', 0), ('tts',   'cloud', 1),
    ('music', 'local', 0), ('music', 'cloud', 1);
