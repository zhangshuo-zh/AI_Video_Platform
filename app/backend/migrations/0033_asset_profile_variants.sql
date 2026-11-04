-- 0033：资产的人物文字档案与命名形态（资产中心增强包）
--
-- 为什么这两张表非建不可（开发说明 §6）：
--   ① `asset.summary` 只有一个 500 字的格子，人物的履历／性格／成长过程塞进去
--      只会互相挤掉，而且简介在镜头选择器里是要当标题读的，不能变成三段小作文；
--   ② `asset.ref_images` 承载不了"这张图是哪套衣服"。把日常常服、雨夜外套、
--      受伤状态无差别塞进同一个数组，下游 I2V 会把互相矛盾的服装当成同一组
--      参考图去生——那比没有参考图更糟。所以形态单独一张表，图跟着形态走。
-- 两张表都只由资产中心写（单实体单写入方）；创作中心与抽取只引用资产 id。
--
-- 老资产一条都不用回填：profile 读不到行就回空档案（不是缺数据，是"还没写"），
-- 形态读不到就是零个形态，基础参考图照旧是 `asset.ref_images`。
-- 这一条是兼容性的底线，测试里钉住了：升级后旧资产的详情、封面、删除保护全不变。

-- 人物专属的文字设定。主键就是 asset_id：一个资产最多一份档案，
-- 不需要再建一个 id 列，也不需要 (asset_id) 唯一索引去兜"多份档案"这种不该存在的状态。
-- text_prompt 与 asset.consistency_prompt 是两码事，且必须一直是两码事：
-- 前者是"让文本模型怎么写这份设定"的输入，后者是"让图像模型画稳定"的输入。
-- 混用一列的结果是改履历就把定妆图的依据也改了，谁都不敢动。
CREATE TABLE IF NOT EXISTS asset_character_profile (
    asset_id        INTEGER PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
    biography       TEXT NOT NULL DEFAULT '',
    personality     TEXT NOT NULL DEFAULT '',
    growth_arc      TEXT NOT NULL DEFAULT '',
    text_prompt     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 命名形态：人物的服饰、场景的视角、道具的形态。
-- variant_type 用 CHECK 收死四个值，和 asset.kind 同一套纪律（0012 就是这么写的）；
-- 类型与父资产是否匹配在**服务层**判：character→outfit、scene→scene_view、
-- prop→prop_form，other 三类通用。不写进 DDL 的 CHECK 是因为那需要跨表相关子查询，
-- SQLite 的 CHECK 里不能引用别的表，硬做只能上触发器——为一个校验养一个触发器不值。
--
-- source_job_id 只用于追溯"这批图是哪次生成来的"，实时状态与成本一律回生成中心读
-- （开发说明 §6.2：不在形态里存 Job 状态的副本）。ON DELETE SET NULL 跟着既有先例：
-- 0019 audio_clip、0021/0022 验证与产物审核、0026 产线节点、0027 成片都是这一条——
-- 来源作业被清掉时，产物本身与这份记录都该留着，只是断了那一条追溯链。
--
-- ref_images 与 asset.ref_images 同构：JSON 数组文本，存 FILES_DIR 下的相对路径，
-- 路径一律服务端生成（uuid 文件名 + paths.resolve_within 边界），不接受客户端传路径。
CREATE TABLE IF NOT EXISTS asset_variant (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id       INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    variant_type   TEXT NOT NULL CHECK (variant_type IN ('outfit','scene_view','prop_form','other')),
    name           TEXT NOT NULL,
    prompt         TEXT NOT NULL DEFAULT '',
    ref_images     TEXT NOT NULL DEFAULT '[]',
    source_job_id  INTEGER REFERENCES job(id) ON DELETE SET NULL,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 同一资产里，同一种形态类型下不许重名：两个"雨夜外套"没法回答"要参考哪一套"。
-- 带上 variant_type 是为了允许"人物的一件衣服"和"一个 other 备选"叫同一个名字，
-- 那种情况本来就不是同一个槽位里的竞争者。
CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_variant_name
    ON asset_variant(asset_id, variant_type, name);
-- 详情页与创作页都是"按资产取全部形态、按 sort_order 排"，一条索引够用；
-- 没顺手加 (source_job_id)：反查"这次生成分散到了哪些形态"是排障动作，不是热路径。
CREATE INDEX IF NOT EXISTS idx_asset_variant_asset_order
    ON asset_variant(asset_id, sort_order, id);
