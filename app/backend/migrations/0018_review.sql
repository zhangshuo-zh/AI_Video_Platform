-- 0018：审核中心（W14）
--
-- 校验器 gate 与人工签字都落在这两张表。两条硬约束：
--   * 每次 gate 绑定文档当时的正文版本（base_version），正文一改，
--     旧报告就标成 stale，绝不会被当成"新版本已通过"；
--   * 签字按锚点唯一（同一版同一锚点只有一条），锚点是正文里的稳定位置
--     （句子偏移），不是数组下标——重跑 gate 不会把签字悄悄挪到别的句子上。
CREATE TABLE IF NOT EXISTS review_gate (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    doc_id        INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    base_version  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('passed','failed')),
    findings_json TEXT NOT NULL DEFAULT '[]',
    summary_json  TEXT NOT NULL DEFAULT '{}',
    created_by    INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_review_gate_doc
    ON review_gate(doc_id, id DESC);

CREATE TABLE IF NOT EXISTS review_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    doc_id        INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    gate_id       INTEGER REFERENCES review_gate(id) ON DELETE SET NULL,
    base_version  TEXT NOT NULL,
    anchor        TEXT NOT NULL,
    decision      TEXT NOT NULL
                  CHECK (decision IN ('approved','rejected','returned')),
    note          TEXT NOT NULL DEFAULT '',
    signed_by     INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    signed_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 同一份文档、同一版、同一个锚点只有一条签字：重签是改判，不是叠一条历史。
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_item_anchor
    ON review_item(doc_id, base_version, anchor);
