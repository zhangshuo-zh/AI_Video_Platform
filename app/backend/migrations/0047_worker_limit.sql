-- 0047：并发上限可以从网页改（2026-11-09）
--
-- 为什么新开一张表而不是塞进 config 或 provider_config：
-- 并发上限是**单例**配置（一台机器一套），而 provider_config 是每能力轨一行、
-- config_change_log 只追加不承载现值。塞进任何一张现成表都会让它的语义变混：
-- 一张表同时表达"每个能力选哪个模型"和"这台机器能跑几条"，读的人得先知道
-- 哪几行是配置、哪几行是例外。
--
-- 只有一行：`id = 1`，`CHECK` 把它钉死。多行会让"到底哪行生效"变成新的猜测点，
-- 而这张表存在的意义恰恰是消除猜测。
--
-- 值可以为 NULL，语义是"没人在网页上设过，按进程启动时的默认/命令行参数走"。
-- 这一点很重要：NULL 与 `global=1` 不是一回事——前者表示"跟随 worker 自己的
-- 判断"，后者表示"人明确要求 1"。把它俩混成一个值，以后就分不清"默认值"和"人设的值"。
--
-- `updated_by` 不设外键：删了账号之后这行还得 standalone 地站着，说明"最后一次
-- 是谁改的"。与 config_change_log 的纪律一致（0044 注释第 3 条）。

CREATE TABLE IF NOT EXISTS worker_limit (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    global_max  INTEGER,
    per_json    TEXT    NOT NULL DEFAULT '{}',
    updated_at  TEXT,
    updated_by  INTEGER,
    note        TEXT    NOT NULL DEFAULT ''
);
