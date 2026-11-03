-- 0025：审核对象多态化（产线阶段 A · 裁定 D1 甲案）
--
-- 为什么要动这张表：审核中心到今天只认 document——`review_item.doc_id NOT NULL`，
-- 签字锚点是正文里的字符偏移。产线要的「关键帧验收」「镜头审核」审的是一件产物，
-- 产物没有正文，也就没有锚点，今天**无处可签**。
--
-- 不变量 3 说 Review 只有一个写入口（审核中心）。所以扩的是审核中心自己的表，
-- 不是在产线里另记一份结论——后者会立刻出现第二个审核事实来源。
--
-- 改法：把"审的是谁"从 `doc_id` 一列换成 `target_kind + target_id` 两列。
--   * document：target_id 就是 document.id，base_version 仍是正文 sha256；
--   * job：target_id 是 job.id，base_version 是**产物清单摘要**（路径＋字节数的 sha256），
--     产物被清理或被新一次生成顶掉，旧签字自然失配——和文档改一个字就失配同一条道理。
-- 锚点对 job 用 `artifact`（整条作业）或 `artifact:<下标>`（某一件产物）。
--
-- 只有两档而不是三档（不设 shot）：镜头审核审的永远是"这一镜那条作业产出的东西"，
-- 落在 job 上就够了；再加一档 shot 会让同一件产物有两个可签对象。
--
-- SQLite 改不了列的 NOT NULL，只能整表重建（0022 同一套写法）：
-- 建新表 → 搬数据 → 删旧表 → 改名 → 重建索引。migrate.py 在迁移期间关掉外键。
-- 代价：`target_id` 是多态列，建不了外键，所以删文档时的级联要在
-- `repositories/documents.delete()` 里显式补一句（已同步改）。项目级联不受影响，
-- `review_item.project_id` 仍是 ON DELETE CASCADE。

CREATE TABLE review_item_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    target_kind   TEXT NOT NULL DEFAULT 'document'
                  CHECK (target_kind IN ('document','job')),
    target_id     INTEGER NOT NULL,
    gate_id       INTEGER REFERENCES review_gate(id) ON DELETE SET NULL,
    base_version  TEXT NOT NULL,
    anchor        TEXT NOT NULL,
    decision      TEXT NOT NULL
                  CHECK (decision IN ('approved','rejected','returned')),
    note          TEXT NOT NULL DEFAULT '',
    signed_by     INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    signed_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 逐列点名搬运：历史签字一条不丢，全部归到 document 这一档。
INSERT INTO review_item_new
    (id, project_id, target_kind, target_id, gate_id, base_version, anchor,
     decision, note, signed_by, signed_at)
SELECT id, project_id, 'document', doc_id, gate_id, base_version, anchor,
       decision, note, signed_by, signed_at
FROM review_item;

DROP TABLE review_item;
ALTER TABLE review_item_new RENAME TO review_item;

-- 同一个对象、同一版、同一个锚点只有一条签字：重签是改判，不是叠一条历史。
-- 原来这条唯一索引是 (doc_id, base_version, anchor)，换成多态键。
CREATE UNIQUE INDEX uq_review_item_target
    ON review_item(target_kind, target_id, base_version, anchor);
-- 审核中心的队列按项目取；产物审核那一栏还要按档筛。
CREATE INDEX idx_review_item_project
    ON review_item(project_id, target_kind, id DESC);
