-- 0020：云端账单核对与重试链（P0 真实性与稳定性）
--
-- 为什么这些列放在 job 上，而不是另建一张"核对表"：
-- 一笔钱属于哪一次调用，是 Job 行自己的事实；拆出去会让成本汇总与作业详情各拿一半，
-- 两边都能单独改——正是这个项目一直在避免的"第二个事实来源"。
-- 验证历史不一样：它是系统级证据，不跟项目生死，所以单独建在 0021。

ALTER TABLE job ADD COLUMN actual_cost_micros INTEGER;
ALTER TABLE job ADD COLUMN reconciled_by INTEGER REFERENCES app_user(id) ON DELETE SET NULL;
ALTER TABLE job ADD COLUMN reconciled_at TEXT;
ALTER TABLE job ADD COLUMN reconcile_note TEXT NOT NULL DEFAULT '';
ALTER TABLE job ADD COLUMN attention_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE job ADD COLUMN superseded_by_job_id INTEGER REFERENCES job(id) ON DELETE SET NULL;

-- 重试链要能从原行找到"后来被哪一行顶掉了"。retry_of_job_id 只有新行知道来源，
-- 一串 5 次重试在界面上就只剩 5 条互不相干的待核对记录，钱看着像重复计了 5 遍。
CREATE INDEX idx_job_superseded ON job(superseded_by_job_id);
CREATE INDEX idx_job_reconciled ON job(billable_status, reconciled_at);
