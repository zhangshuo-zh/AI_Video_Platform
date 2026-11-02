-- 0023：摘掉两个没有写入方的状态列（`shot.status`、`asset.status`）
--
-- 为什么删而不是补：整体风险这一轮点出来这两列表里有、代码里没人写。
-- `shots.set_status()` 定义了但零调用方，资产那一列连写入路径都没有。
-- 两条路里选摘掉，理由是**补写入要先定义第二个状态机**：
--   · 镜头的"todo|queued|done|failed"说的其实是"这条镜头生成过没有"，
--     而那份事实已经在 `job` 表里（最新一条作业的状态），
--     创作中心显示的就是它——再加一列等于两个真相源，早晚对不上；
--   · 资产的"draft|ready"要谁在什么时刻把它推成 ready？没有这个流程，
--     资产中心的状态一直靠引用数与是否有定妆图现算。
-- 界面早就按这个口径走了：`writing.js` 里写着"不用 shot.status——
-- 那一列没有写入方，拿它显示等于让界面说谎"。列留着只会让人以为它有含义。
--
-- SQLite 3.35 起支持 DROP COLUMN。这两列都不在索引、CHECK、外键或视图里
-- （`uq_asset_name`／`idx_shot_episode`／`idx_asset_project` 都不含 status），
-- 所以不用像 0007／0012 那样整表重建。
-- 有历史数据也不怕：这两列从来没有被写过，删掉的只是默认值。

ALTER TABLE shot DROP COLUMN status;
ALTER TABLE asset DROP COLUMN status;
