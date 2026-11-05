-- 0037：项目生产工作台的三件地基
--
-- 为什么需要这一支迁移，而不是把东西塞进前端：
--
-- 1) `user_context`——任务书 §4.1 要"当前项目 × 当前集"成为跨模块上下文。
--    前端 `store.currentId` 只活一次页面会话，"当前集"更是各页各记
--    （`navstate` 的 sub/param）。刷新一次、换一个浏览器，"我正在做第 3 集"就没了；
--    工作台、预检、审核三处会各说一个集。上下文必须只有一个东家，而这个东家
--    只能在服务端——所以每人一行，跟着登录态走。
--    注意它**不是权限**：谁能看这个项目仍由 `project_member` 与权限点决定，
--    这里只存"他上次站在哪一集"。读的时候照样过可见性判断。
--
-- 2) `shot` 的镜头设定字段——任务书 §6.1 第一阶段要展示与编辑景别／运镜／
--    画面比例／镜头风格／负面提示词，而现在 `shot` 只有
--    summary／prompt／duration／asset_ids／variant_ids。
--    一律 NOT NULL DEFAULT ''：可空会让每个读方都要判 None，
--    而"没填"和"填了空串"对界面是同一件事。
--    **这五列不进 `shot_source_version()` 的指纹**：今天没有任何执行器读它们，
--    把它们塞进指纹等于"补填一个景别 → 全片产物判过期 → 签字集体失效"。
--    将来真把它们喂给模型时，再连同一次显式的历史过期一起改指纹。
--
-- 3) 不新建产物表、不加"正式关键帧／正式视频"指针列：
--    这两件事现在由"签字成功的那条 job"表达（`review_item(target_kind='job')`
--    ＋ `job.status='succeeded'`），`composition/timeline.build()` 就是按它取片的。
--    再加一个指针列就是给同一个事实找第二个东家，两边迟早说不一样的话。

CREATE TABLE IF NOT EXISTS user_context (
    user_id     INTEGER PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    project_id  INTEGER REFERENCES project(id) ON DELETE SET NULL,
    episode_id  INTEGER REFERENCES episode(id) ON DELETE SET NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

ALTER TABLE shot ADD COLUMN shot_size       TEXT NOT NULL DEFAULT '';
ALTER TABLE shot ADD COLUMN camera_move     TEXT NOT NULL DEFAULT '';
ALTER TABLE shot ADD COLUMN aspect          TEXT NOT NULL DEFAULT '';
ALTER TABLE shot ADD COLUMN style           TEXT NOT NULL DEFAULT '';
ALTER TABLE shot ADD COLUMN negative_prompt TEXT NOT NULL DEFAULT '';
