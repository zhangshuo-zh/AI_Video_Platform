-- 0046：补两条被 bench 与 EXPLAIN 点过名、但一直没落地的索引
--
-- 两条都不是"猜着想加"，各有一个具体的读路径与实测依据：
--
-- ① idx_audio_episode ON audio_clip(episode_id, status)
--    读路径：生产工作台每打开一集都会问一次
--      `SELECT kind, status, COUNT(*) FROM audio_clip
--        WHERE episode_id = ? GROUP BY kind, status`（repositories/production.py）
--    与"这一集的镜头还有哪些没配音"那条 NOT EXISTS（同一文件下方）。
--    现役索引只有 `(project_id, kind, id DESC)` 与 `(shot_id)`、`(job_id)`，
--    没有一条以 episode_id 打头，于是这两句都是**全表扫 audio_clip**。
--    `tools/bench_queries.py` 早就把这条列进了 CANDIDATE_INDEXES
--    （`audio_episode` → `audio_clip(episode_id, status)`），但一直停在候选名单里，
--    现役 schema 没采纳——这就是"测过但没落地"。这里按 bench 的口径补上：
--    status 放第二列，正好覆盖 `GROUP BY kind, status` 的过滤段。
--
-- ② idx_review_gate_project ON review_gate(project_id, doc_id, id DESC)
--    读路径：项目看板那条
--      `SELECT status, COUNT(*) FROM review_gate g
--        WHERE project_id = ? AND g.id = (SELECT MAX(id) FROM review_gate
--                                          WHERE doc_id = g.doc_id) GROUP BY status`
--    （repositories/dashboard.py）。现役只有 `idx_review_gate_doc(doc_id, id DESC)`，
--    没有 project_id 打头的索引。用内存库跑 EXPLAIN QUERY PLAN，形状是
--    `SCAN g` + `CORRELATED SCALAR SUBQUERY` + `USE TEMP B-TREE FOR GROUP BY`：
--    外层是整表扫，只有相关子查询走了覆盖索引。gate 表随每次校验器运行只增
--    不减（没有清理任务），所以这个全表扫只会越来越贵。
--    索引里带上 `doc_id, id DESC`，让"按 doc 取最新一条"那一步也能在同一棵 B 树上走完。
--
-- 写代价说清楚：audio_clip 与 review_gate 都是低频小表（一集几十条音轨、
-- 一份文档几十次校验），每次 INSERT/UPDATE 多维护一棵 B 树在这个量级下
-- 不构成瓶颈；而读侧是"每打开一集/一个项目都要全表扫"，两边不对称，所以该加。
--
-- 没有顺手加 `job(project_id, status)`：0032 的注释明确论证过当时不加的理由
-- （每个项目几十行，临时 B 树分组不贵），要推翻那个判断得先在目标机用
-- `bench_queries.py` 量出来，不在这一轮里做。

CREATE INDEX IF NOT EXISTS idx_audio_episode
    ON audio_clip(episode_id, status);

CREATE INDEX IF NOT EXISTS idx_review_gate_project
    ON review_gate(project_id, doc_id, id DESC);
