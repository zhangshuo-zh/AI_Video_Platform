-- 项目标签：一个项目可以挂多个标签，用来检索。
-- 单开一张表而不是塞 JSON 列：标签要能按值反查（"所有带 短剧 的项目"），
-- 将来还能统计每个标签下有多少项目。
CREATE TABLE IF NOT EXISTS project_tag (
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    PRIMARY KEY (project_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_project_tag ON project_tag(tag);
