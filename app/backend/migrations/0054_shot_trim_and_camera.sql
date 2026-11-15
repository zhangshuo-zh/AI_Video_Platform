-- 0054: 分镜出入点粗剪（trim_in_ms / trim_out_ms）与转场类型（transition）。
-- 默认值 0 / 0 / 'cut' 表示不截取与直接硬切，保持既有镜头与成片行为 100% 向后兼容。
ALTER TABLE shot ADD COLUMN trim_in_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shot ADD COLUMN trim_out_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shot ADD COLUMN transition TEXT NOT NULL DEFAULT 'cut';
