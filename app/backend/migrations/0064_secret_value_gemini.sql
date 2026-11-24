-- 0064：在供应商密钥表 CHECK 白名单中增加 GEMINI_API_KEY（2026-11-24）
-- 供资产参考图看图反推（Google Gemini）与后续多模态能力复用平台加密存储。

CREATE TABLE secret_value_new (
    env_name    TEXT PRIMARY KEY CHECK (env_name IN (
                  'DEEPSEEK_API_KEY',
                  'DASHSCOPE_API_KEY',
                  'GEMINI_API_KEY',
                  'FV_KEY_JIMENG',
                  'FV_KEY_KLING',
                  'FV_KEY_MINIMAX',
                  'FV_KEY_SEEDANCE',
                  'FV_KEY_VOLC',
                  'FV_KEY_WANX'
                )),
    ciphertext  BLOB NOT NULL,
    nonce       BLOB NOT NULL,
    aad         BLOB NOT NULL,
    fingerprint TEXT NOT NULL DEFAULT '',
    length      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT,
    updated_by  INTEGER,
    note        TEXT NOT NULL DEFAULT ''
);

INSERT INTO secret_value_new (env_name, ciphertext, nonce, aad, fingerprint, length, updated_at, updated_by, note)
SELECT env_name, ciphertext, nonce, aad, fingerprint, length, updated_at, updated_by, note
FROM secret_value;

DROP TABLE secret_value;
ALTER TABLE secret_value_new RENAME TO secret_value;
