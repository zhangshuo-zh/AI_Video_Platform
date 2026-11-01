-- W05 文档层：数据库只存"文档这份元数据"，正文留在 workspace 里走 Git。
--
-- 为什么正文不进 SQLite（规格 02 §1.5／数据字典 §1 第 1 条）：
-- 正文的事实来源只能有一个。进了库就变成"库和文件各一份、谁也不认谁"，
-- Git 那边有历史、库这边也有历史，冲突时没人说得清哪个是准的。
-- 库里存的是"这份文档归谁、到哪个阶段、当前是哪一次提交"，
-- 靠 git_commit 把两边钉在一起（Review 也绑这个值，规格 02 §9）。
--
-- 新建表，不动任何既有表：不需要 SQLite 重建那一套（0005／0007／0010／0012 才需要）。
CREATE TABLE document (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 文档一定挂项目：路径、可见性、删除级联都按项目算。
    -- 不做"全局文档池"——资产有复用的需求，小说/剧本没有。
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 可空：全项目共用的一份（总纲、人物小传）不该被塞进某一集。
    -- 数据字典 §6.1 就是这个口径：大纲/设定可空，章节文档指向集。
    episode_id  INTEGER REFERENCES episode(id) ON DELETE CASCADE,
    -- 三级文档。storyboard 先留在枚举里但界面上不开放：
    -- 分镜的真相是 shot 表（一镜一行、要挂生成作业），再存一份 Markdown 就是第二个事实来源。
    -- 留着这一档是为了将来决定"分镜要不要出一份人读的稿子"时不用改表。
    kind        TEXT NOT NULL CHECK (kind IN ('novel','script','storyboard')),
    title       TEXT NOT NULL,
    -- 工作区内的相对路径，绝对路径一律不收（workspace.py 里校验）。
    -- UNIQUE 是全局唯一而不只是项目内唯一：Git 仓库是整个 workspace 共用一个，
    -- 两个项目占同一条路径等于在同一个仓库里互相覆盖，那才是真事故。
    rel_path    TEXT NOT NULL UNIQUE,
    -- 草稿 → 润色 → 定稿（数据字典 §7 推荐值；库里加 CHECK，
    -- 因为这三档是流程状态，写错一档会让项目中心的进度显示说不通）
    status      TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','polishing','finalized')),
    -- 当前正文对应的那次提交。NULL 只有一种可能：Git 坏了没提交成功，
    -- 那种情况下保存接口是报错回来的，不会留下"看起来定了稿"的行。
    git_commit  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 列表页永远是"某项目下某类型的文档"，直接走索引
CREATE INDEX idx_document_project ON document(project_id, kind, title);
-- 删一集要把那几份文档的挂靠关系查出来（episode_id 是 CASCADE，行会跟着走，
-- 但界面上"这一集还有 3 份文档"的提示要按它查）
CREATE INDEX idx_document_episode ON document(episode_id);

-- 编辑锁。单独一张表而不是 document 上加两列（数据字典 §6.1 给了两种写法）：
-- 1. 锁是"临时状态"，和元数据混在一张表里，改一次心跳就要 touch 一次 updated_at，
--    而 updated_at 的语义是"内容动过"——项目中心那列会被心跳刷花（v0.62 才刚把它立起来）。
-- 2. 一张表一行一把锁，主键就是文档 id，天然不允许一个人之外的第二把锁存在。
-- 回收策略见 repositories/documents.py：过期行不删，由下一次 acquire 覆盖，
-- 省掉一个定时任务（2—5 人、锁 15 分钟，攒不下几行）。
CREATE TABLE doc_lock (
    doc_id         INTEGER PRIMARY KEY REFERENCES document(id) ON DELETE CASCADE,
    -- CASCADE 到人是必须的：删账号时如果 RESTRICT，会出现"号删不掉因为他还锁着别人的文档"。
    -- 账号没了，这把锁自然就该没了。
    holder_user_id INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    acquired_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expires_at     TEXT NOT NULL
);

-- 超时回收时按到期时间清理（也方便将来真要加个 purge 时不用全表扫）
CREATE INDEX idx_doc_lock_expiry ON doc_lock(expires_at);
