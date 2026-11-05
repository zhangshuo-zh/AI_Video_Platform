-- 0038：审核质量清单（任务书 §8.2／§12.1，P1 第 4、5 项）
--
-- 为什么要这张表：产物审核今天只有一枚结论（approved／rejected／returned），
-- 一句 note。可 §8 要的是"审核不仅检查文件，也检查内容质量"——画面跟不跟提示词、
-- 人物形态对不对、有没有坏帧、台词能不能对上，这些**是逐条的判断**，
-- 而结论压成一条 note 之后，两个人复审同一条作业时看不出上一版卡在哪儿。
-- 清单要能被复查、能被批量审计时数一数，就得有一行一条目的落库处。
--
-- 三条设计决定：
--
-- 1) **清单不替代签字**。结论仍然只有 `review_item` 那一个写入口（不变量：签字只落一处）。
--    这张表存的是"你看过哪几项、分别判了什么"，`review_item` 存的才是"这一版能不能用"。
--    两者版本口径一致：都绑当前产物清单摘要（`base_version`），
--    所以产物一换，清单与签字一起失配，不会出现"清单是新的、结论是旧的"。
--
-- 2) **多态目标与 `review_item` 同法**：`target_kind` + `target_id`，不给 `target_id` 上外键。
--    作业与文档都可能有清单，而上外键就得建两条支路，那正是 `review_item` 当年避开的坑。
--    `project_id` 照常带外键并 CASCADE——项目删了，清单没有留下来的理由。
--
-- 3) **只存人给的结论，不存自动项**。自动项（文件在不在、时长对不对、源版本变没变）
--    每次读的时候现算，来源分别是 `jobfiles`、`composition.validators.inspect`
--    与 `services.job_source_stale`。存下来就等于养第二份事实，而它会过期。
--    `code` 因此只允许人工清单里那几项（枚举在 `reviewkit.py`，一处定义）。

CREATE TABLE IF NOT EXISTS review_check (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('job', 'document')),
    target_id    INTEGER NOT NULL,
    code         TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('pass', 'issue', 'na')),
    note         TEXT NOT NULL DEFAULT '',
    checked_by   INTEGER NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
    checked_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    base_version TEXT NOT NULL DEFAULT '',
    -- 同一目标同一项只留一条：改判覆盖，与 `review_item` 的改判口径一致，
    -- 否则复审历史会长成一张没人读的流水账（历史要看的是签字，不是勾选）。
    UNIQUE (target_kind, target_id, code)
);

-- 审核中心一页要数"这一批还缺几项"，按目标一次读；项目侧的聚合走 project_id。
CREATE INDEX IF NOT EXISTS idx_review_check_target
    ON review_check (target_kind, target_id);
CREATE INDEX IF NOT EXISTS idx_review_check_project
    ON review_check (project_id);
