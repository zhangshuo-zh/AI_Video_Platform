-- 0049：站内通知（2026-11-10）
--
-- 为什么新开一张表而不是复用 `config_change_log`：那张表回答的是"配置怎么变成
-- 现在这样"（只追加、不面向人、没有已读概念）；通知要回答的是"有哪些事需要
-- 我看一眼、我看过了没有"。两者一旦混用，config_log 的追加语义会被已读标记
-- 破坏（UPDATE 一张"只追加"的表），而通知也会被塞进大量与己无关的配置变更。
--
-- 收件人怎么定（`audience`）是这张表最关键的一列。三种：
--   'user:<id>'       点名某个人（如"你提交的作业失败了"）
--   'project:<id>'    某个项目的所有者与协作者（**这是本需求要的那种**：
--                     worker 掉了、某个项目有作业受影响 → 该项目的人都知道）
--   'config_admins'   有 system:config 的人（守护动作、并发越界这类整机事件）
--
-- 为什么不把收件人展开成多行：项目成员会变（加人/删人），展开的话
-- "刚加入的人看不到历史通知"还算合理，但"退出的成员仍保留着他的副本"就是
-- 数据泄漏。存成"指向项目"再由查询期解析成当前成员，退出的自然看不到了。
--
-- `read_at` 按人记录，所以另开 `notification_read`：一条通知被 3 个项目成员
-- 看到，各自标记已读互不影响。不塞进 notification 一列，否则只有一个人
-- 的已读状态能被记住。

CREATE TABLE IF NOT EXISTS notification (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    level       TEXT    NOT NULL DEFAULT 'info',
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    -- 点击跳转：前端只认这两个，不存整条 URL（域名/端口会变，路径不会）
    link_page   TEXT    NOT NULL DEFAULT '',
    link_params TEXT    NOT NULL DEFAULT '{}',
    audience    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- 同一件事不重复打扰：同一 kind+audience+dedupe_key 在未读期间只留一条。
    -- 例：worker 掉线每 15 秒检查一次，不能每轮都发一条新的。
    dedupe_key  TEXT    NOT NULL DEFAULT ''
);

-- 未读列表按时间倒序取，这个索引直接服务那条查询
CREATE INDEX IF NOT EXISTS idx_notification_created
    ON notification(created_at DESC, id DESC);
-- 去重查询要按 (kind, audience, dedupe_key) 找最近的未结条目
CREATE INDEX IF NOT EXISTS idx_notification_dedupe
    ON notification(kind, audience, dedupe_key);

CREATE TABLE IF NOT EXISTS notification_read (
    notification_id INTEGER NOT NULL REFERENCES notification(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL,
    read_at         TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (notification_id, user_id)
);
