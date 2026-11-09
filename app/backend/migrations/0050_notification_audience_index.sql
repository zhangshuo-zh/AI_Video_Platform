-- 0050：通知收件箱的 audience 查询缺一条索引（2026-11-10 自查）
--
-- 读路径：顶栏铃铛每 20 秒拉一次 `/api/notifications`，SQL 形如
--
--   SELECT n.* FROM notification n
--     LEFT JOIN notification_read r ON r.notification_id = n.id AND r.user_id = ?
--    WHERE (n.audience = 'user:1' OR n.audience = 'project:8' OR n.audience = 'config_admins')
--    ORDER BY n.id DESC LIMIT 30
--
-- 0049 建的两条索引是 `(created_at DESC, id DESC)` 与 `(kind, audience, dedupe_key)`。
-- 前者服务于"按时间取最新"，但那条 ORDER BY 在这里被 OR 条件挡在索引之外；
-- 后者以 kind 打头，而这条查询**不带 kind**，用不上。
-- 于是每次拉通知都是**全表扫 notification**——表会一直长（每条掉线/守护动作一行），
-- 而这是每个登录者每 20 秒都会打的接口（与 `worker_status` 同一类放大）。
--
-- 加 `(audience, id DESC)`：OR 里的每个等值条件都能单独走它，
-- 再按 id 倒序取前 N 条，不必把整表读出来排序。

CREATE INDEX IF NOT EXISTS idx_notification_audience
    ON notification(audience, id DESC);
