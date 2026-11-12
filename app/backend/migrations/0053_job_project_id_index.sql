-- 按项目读取最近 Job 使用 id DESC；原 (project_id, queued_at DESC)
-- 无法满足该顺序，会对项目内全部记录解析归属并用临时 B-tree 排序。
-- 临时库 100000 条作业、每类查询 5 次：项目历史中位 118.656→0.584ms，
-- 活跃资产历史 117.974→0.724ms，结果 id 逐项一致。
-- 未命中资产仍须扫描项目历史，不以本索引宣称归属检索恒定复杂度。
CREATE INDEX IF NOT EXISTS idx_job_project_id ON job(project_id, id DESC);
