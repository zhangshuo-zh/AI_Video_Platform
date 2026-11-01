-- 资产中心封版（本次开发指令）：资产可以不属于任何项目＝全局资产池，并补 aliases 与标签。
--
-- project_id 从 NOT NULL 改成可空，SQLite 没有 ALTER COLUMN，只能重建整表。
-- 沿用 0005／0007／0010 的重建写法：迁移期间的外键由 migrate.py 统一关掉，
-- 跑完再 foreign_key_check，一个迁移一个事务，失败完全回滚。
CREATE TABLE asset_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL = 全局资产池。这一条本身就是「删项目不能删掉全局资产」的全部机制：
    -- 全局行的 project_id 是 NULL，ON DELETE CASCADE 匹配不到它，不需要额外写判断。
    -- 所有权与引用关系就此分开：资产行属于谁只有一个答案，项目要用它就把 id 记进
    -- shot.asset_ids，不复制一份（指令 §5.3「引用全局资产不应自动复制」）。
    project_id         INTEGER REFERENCES project(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL CHECK (kind IN ('character','scene','prop')),
    name               TEXT NOT NULL,
    -- 别名。抽取草案 §2.4 的 asset 定义里就有这一列，而现有表没有；
    -- 将来 accept/merge 靠它把"提灯的老者"并回"掌灯人"。现在先补上，
    -- 免得接抽取那天再动一次资产表。
    aliases            TEXT NOT NULL DEFAULT '[]',
    summary            TEXT NOT NULL DEFAULT '',
    consistency_prompt TEXT NOT NULL DEFAULT '',
    ref_images         TEXT NOT NULL DEFAULT '[]',
    status             TEXT NOT NULL DEFAULT 'draft',
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

INSERT INTO asset_new (id, project_id, kind, name, aliases, summary, consistency_prompt,
                       ref_images, status, created_at, updated_at)
SELECT id, project_id, kind, name, '[]', COALESCE(summary, ''),
       COALESCE(consistency_prompt, ''), COALESCE(ref_images, '[]'),
       COALESCE(status, 'draft'), created_at, updated_at
  FROM asset;

-- 旧表上的 uq_asset_name / idx_asset_project 随 DROP 一起消失，下面重建。
DROP TABLE asset;
ALTER TABLE asset_new RENAME TO asset;

-- 唯一性拆成两条部分索引，这是本次迁移里唯一不显然的一处。
-- 为什么不用原来那条 (project_id, kind, name)：SQLite 认为 NULL 彼此不相等，
-- 于是两条同名的全局资产（project_id 都是 NULL）它判不冲突，重名拦不住。
-- 指令 §4.2 要的是"同归属范围、同类型内唯一"，那就按范围各写一条。
CREATE UNIQUE INDEX uq_asset_name ON asset(project_id, kind, name)
    WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX uq_asset_global_name ON asset(kind, name)
    WHERE project_id IS NULL;

CREATE INDEX idx_asset_project ON asset(project_id, kind, name);
CREATE INDEX idx_asset_kind ON asset(kind, name);

-- 资产标签。照 project_tag 的样子单开一张表而不是塞 JSON 列：
-- 指令 §3.1-C 要"搜索支持名称和标签"，按值反查与将来统计都得走索引。
CREATE TABLE asset_tag (
    asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    PRIMARY KEY (asset_id, tag)
);
CREATE INDEX idx_asset_tag ON asset_tag(tag);
