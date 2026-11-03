-- 0029：多 worker 心跳（阶段 E）
--
-- 原来的 worker_state 只有一行（id=1）：单 worker 时代"谁在跑"只有一个答案。
-- 阶段 E 允许多开 worker 各领一条不同资源类的作业，就必须能列出每一个进程，
-- 否则顶栏只能回答"有没有人活着"，回答不了"几条腿在干活、各自领了什么"。
--
-- 边界：
--   * `limits_json` 记这个进程启动时的并发上限（全局／每资源类），是"它凭什么
--     领活"的证据，不是平台当前配置——改配置要重启 worker 才生效。
--   * `current_job_id` 只引用 job；作业被删时 SET NULL，不让面板跟着 500。
--   * worker_state 保留并继续镜像"最近一次心跳"：旧客户端与备份恢复还认它。

CREATE TABLE IF NOT EXISTS worker_node (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id      TEXT NOT NULL UNIQUE,
    pid            INTEGER NOT NULL DEFAULT 0,
    host           TEXT NOT NULL DEFAULT '',
    started_at     TEXT NOT NULL DEFAULT '',
    heartbeat_at   TEXT NOT NULL DEFAULT '',
    current_job_id INTEGER REFERENCES job(id) ON DELETE SET NULL,
    version        TEXT NOT NULL DEFAULT '',
    limits_json    TEXT NOT NULL DEFAULT '{}',
    resources_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_worker_node_heartbeat
    ON worker_node(heartbeat_at DESC);
