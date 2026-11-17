-- 运行监控历史过滤：只索引筛选键与游标，不复制验证事实或大文本。
CREATE INDEX IF NOT EXISTS idx_validation_history_provider ON model_validation(provider, id DESC);
CREATE INDEX IF NOT EXISTS idx_validation_history_model ON model_validation(model, id DESC);
CREATE INDEX IF NOT EXISTS idx_validation_history_time ON model_validation(created_at, id DESC);
