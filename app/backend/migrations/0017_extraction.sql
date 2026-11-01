-- 0017：文本抽取候选（W13）
--
-- 一次抽取仍然是一条 Job：成本只从 job 表汇总，不另开一个"抽取花费"的口子。
-- 这里只存这次抽取独有的东西：抽的是哪份文档的哪一版、模型与提示词版本、
-- 以及候选本身。候选不直接进 asset——人点确认才落资产，是这一包的安全边界。
--
-- 两个版本号不能省：
--   * base_version：正文内容的 sha256，和 W05 的保存令牌同一个算法；
--   * content_hash：送进模型的那份正文的哈希，去重与复现都用它。
-- 两者在提交时相等；之后正文再改，base_version 仍指着旧那一份，
-- 于是"证据偏移对的是哪一版"永远回答得出来。
CREATE TABLE IF NOT EXISTS extraction_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    doc_id          INTEGER REFERENCES document(id) ON DELETE SET NULL,
    job_id          INTEGER NOT NULL UNIQUE REFERENCES job(id) ON DELETE CASCADE,
    doc_title       TEXT NOT NULL,
    base_version    TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    model_code      TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','done','failed','parse_failed')),
    note            TEXT NOT NULL DEFAULT '',
    materialized_at TEXT,
    created_by      INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 去重查询：同一份正文 + 同一模型 + 同一提示词版本，24 小时内不再花钱
CREATE INDEX IF NOT EXISTS idx_extraction_dedupe
    ON extraction_run(doc_id, content_hash, model_code, prompt_version, created_at);
CREATE INDEX IF NOT EXISTS idx_extraction_project
    ON extraction_run(project_id, created_at DESC);

-- 候选。四种 kind 共用一张表：它们的生命周期完全一样（pending → 终态一次），
-- 拆四张表只会把"确认"这条逻辑写四遍。payload 没有字段级约束，
-- 由接口层的白名单补——模型输出不能直接决定表结构。
CREATE TABLE IF NOT EXISTS extraction_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES extraction_run(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL
                CHECK (kind IN ('character','scene','prop','shot')),
    payload     TEXT NOT NULL DEFAULT '{}',
    confidence  REAL,
    evidence    TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','accepted','merged','rejected')),
    asset_id    INTEGER REFERENCES asset(id) ON DELETE SET NULL,
    decided_by  INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    decided_at  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_extraction_item_run
    ON extraction_item(run_id, kind, status);
