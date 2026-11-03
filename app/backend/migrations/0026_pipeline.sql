-- 0026：产线中心（阶段 B · 裁定 D3）
--
-- 这四张表只做一件事：**记住一次生产的流程走到哪了**。
-- 它们不是第二个队列——节点不执行模型、不领取作业、不存 Job 的状态与成本；
-- 真正干活的仍然只有 `job` 表 ＋ 那一个 worker（不变量 2）。
--
-- 为什么还是建了表（系统设计 §16.11 原来写"Task 实体不建"）：
-- 那条说的是"不要建第二张队列表"。产线运行是**编排记录**：谁发起了哪一集的生产、
-- 用的是哪一版流程、每个节点关联哪条 Job、卡在哪一步、失败原因是什么。
-- 这些信息在 Job 表里没有位置（Job 只知道自己是哪一镜的一次调用），
-- 也没有别的表能派生出来。判据只有一条：节点行里**不出现**任何可以从 Job 现读的列
-- （状态、成本、日志、耗时、产物一律不抄），所以它不可能与 Job 打架。
--
-- 三条硬约束写进表结构：
--   * `(run_id, node_key)` 唯一——同一节点被重复触发时，第二条插不进来；
--   * 模板版本冻结在 run 上（`template_version`），改模板不影响已经跑起来的运行；
--   * 运行状态只存"外生意愿"两位（paused／cancelled），其余一律从节点现算——
--     v0.86 修过的两个真 bug 都是"两套状态各自维护"造成的，这里不重犯。

CREATE TABLE IF NOT EXISTS pipeline_template (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    description     TEXT NOT NULL DEFAULT '',
    -- 节点与依赖的定义。发布后不改历史版本：要改就 version+1 再插一行
    definition_json TEXT NOT NULL DEFAULT '{}',
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    -- 内置模板＝1：界面上只读，不许改名改定义（改内置模板要走迁移）
    builtin         INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 同名同版只能有一份；插内置模板时用 INSERT OR IGNORE 兜住重复启动
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_template
    ON pipeline_template(name, version);


CREATE TABLE IF NOT EXISTS pipeline_run (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    -- 一集删了，这集的产线运行就没有意义了，所以 CASCADE。
    -- 注意这与 `job.episode_id` 的 SET NULL 不同：Job 是账，删一集不该把账抹掉；
    -- 运行是流程，流程的对象没了，流程本身也就没了。
    episode_id          INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    template_id         INTEGER NOT NULL REFERENCES pipeline_template(id),
    template_version    INTEGER NOT NULL,
    -- 只存外生意愿：暂停与取消是人按的，其余状态从节点现算
    paused              INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    cancelled           INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1)),
    cancel_reason       TEXT NOT NULL DEFAULT '',
    -- 本次运行的输入快照：剧本版本、镜头四要素、资产与参考图状态。
    -- 运行开始后不再改——"跑的是哪一版剧本"必须回答得出来
    input_snapshot_json TEXT NOT NULL DEFAULT '{}',
    -- 防连点两次起两条整集产线。同项目同集只允许一条未结束的运行，
    -- 那一条由仓储层判；这个键管的是"同一个请求重放"
    idempotency_key     TEXT UNIQUE,
    started_at          TEXT,
    finished_at         TEXT,
    created_by          INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run_project
    ON pipeline_run(project_id, id DESC);
-- tick() 每轮要捞"还没结束的运行"：按这两位筛，不用扫全表
CREATE INDEX IF NOT EXISTS idx_pipeline_run_active
    ON pipeline_run(cancelled, paused, id);


CREATE TABLE IF NOT EXISTS pipeline_node_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    node_key     TEXT NOT NULL,
    -- 稳定枚举，写在 CHECK 里：不让前端传一个字符串就当执行函数名用
    node_type    TEXT NOT NULL CHECK (node_type IN (
                     'project_input','episode_input','document_input',
                     'shot_validate','asset_validate','human_gate',
                     'image_generate','image_review','video_generate',
                     'artifact_validate','shot_review','audio_prepare',
                     'export_prepare')),
    -- 只存真实发生过的事。`blocked`／`ready` 不入库——它们是
    -- "pending ＋ 依赖满不满足"的现算结果，存下来就成了第二套事实
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                     'pending','running','waiting_review','attention',
                     'completed','failed','skipped','cancelled')),
    -- 依赖是模板冻结下来的，运行期不会变，所以存一列就够，不建边表
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    label        TEXT NOT NULL DEFAULT '',
    -- 展开时冻结的输入（镜头 id、提示词、参数、模型档）。
    -- 只放引用与快照，不放业务对象本身（不变量 6）
    input_json   TEXT NOT NULL DEFAULT '{}',
    -- 输出也只放引用：job_id、review_item id、几个计数。不抄产物路径清单
    output_json  TEXT NOT NULL DEFAULT '{}',
    -- 关联现有 Job。只引用：状态、成本、日志、产物一律现读 job 表。
    -- SET NULL 而不是 CASCADE——删一条 Job 不该把节点历史一起抹掉，
    -- 节点还要能说"我关联的作业已经没了"
    job_id       INTEGER REFERENCES job(id) ON DELETE SET NULL,
    -- 人工审核结论的引用（review_item.id）。产线不存结论，只指过去
    review_id    INTEGER REFERENCES review_item(id) ON DELETE SET NULL,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    error_code   TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at   TEXT,
    finished_at  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 附件 §6.3 要的那一条：同一运行内节点标识唯一，重复调度插不进第二行
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_node
    ON pipeline_node_run(run_id, node_key);
CREATE INDEX IF NOT EXISTS idx_pipeline_node_run
    ON pipeline_node_run(run_id, id);
CREATE INDEX IF NOT EXISTS idx_pipeline_node_job
    ON pipeline_node_run(job_id);


-- 事件只追加、不更新：它是"流程走过哪些步"的线索，
-- 不代替 Job 日志，也不代替业务审计（操作审计仍是未实现能力）
CREATE TABLE IF NOT EXISTS pipeline_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pipeline_run(id) ON DELETE CASCADE,
    node_id      INTEGER REFERENCES pipeline_node_run(id) ON DELETE SET NULL,
    event_type   TEXT NOT NULL CHECK (event_type IN (
                     'run_created','run_started','run_paused','run_resumed',
                     'run_cancelled','run_completed','run_failed',
                     'node_ready','node_started','node_completed','node_failed',
                     'node_skipped','node_retried','node_waiting_review',
                     'node_attention','job_submitted')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_event_run
    ON pipeline_event(run_id, id);
