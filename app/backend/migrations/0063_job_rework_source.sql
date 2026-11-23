ALTER TABLE job ADD COLUMN rework_of_job_id INTEGER REFERENCES job(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_job_rework_source ON job(rework_of_job_id) WHERE rework_of_job_id IS NOT NULL;
