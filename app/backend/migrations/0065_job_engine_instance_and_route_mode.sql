-- 保留已删除实例的 ID，执行时能报告绑定失效。
ALTER TABLE job ADD COLUMN engine_instance_id INTEGER;
ALTER TABLE generation_profile ADD COLUMN execution_mode TEXT NOT NULL DEFAULT '';
ALTER TABLE model_policy ADD COLUMN suggested_mode TEXT NOT NULL DEFAULT '';
ALTER TABLE model_policy ADD COLUMN fallback_mode TEXT NOT NULL DEFAULT '';
