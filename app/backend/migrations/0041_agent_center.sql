-- 0041：智能体中心——运行记录三张表，外加签字多一个可签对象
--
-- 这三张表只做一件事：**记住"Agent 替人做到哪一步了"**。
-- 它们不是第二个队列——步骤不执行模型、不领取作业、不存 Job 的状态与成本；
-- 真正干活的仍然只有 `job` 表 ＋ 那一个 worker（不变量 2，同 0026 对产线说的话）。
--
-- 为什么不照《智能体管理中心》说明书 §7 建那六张表（取证见
-- 《文档/开发/智能体管理/00-接入前现状审计.md》§3）：
--   * `agent_provider_config`／`agent_routing_policy` **不建**。前者与
--     `model_registration`（这一档模型在哪）＋ `engine_instance`（在哪个端点）＋
--     `provider_config`（这一类能力走哪条轨）三张重叠，建了就是"这一档在哪"的
--     第四个答案；后者的六列在 `model_policy`（0035）里一一对得上，缺的只有
--     "每次询问"一档。同一件事有两个可写处，正是本项目反复在修的那类缺陷。
--   * `agent_approval` **不建**，扩 `review_item` 的 `target_kind`（下面第四段）。
--     不变量 3 说签字只有审核中心一个写入口；另立一张"待审批"表就会同时存在
--     两处人点头的地方，而它们互不知道对方做了什么。
--   * 剩下 run／step／event 三张是真缺口：这些信息在 Job 表里没有位置
--     （Job 只知道自己是某一次调用），也没有别的表能派生出来。
--
-- 判据与 0026 同一条，而且是可检查的：**step 行里不出现任何能从 Job 现读的列**。
-- 状态、成本、日志、耗时、产物一律去 `job` 表读；这里只存"这一步关联哪条 Job、
-- 卡在哪、失败时人看到的那句话"。所以智能体与 Job 不可能长出两套执行事实。
--
-- 三条硬约束写进表结构：
--   * `(run_id, seq)` 唯一——计划重放、worker 重启、同一请求发两次都插不进第二步；
--   * `args_hash` 不是装饰：它是审批的绑定对象（说明书 §3.4「审批与参数绑定」）。
--     签字时把它抄进 `review_item.base_version`，参数一改哈希就变，旧审批自然失配——
--     和 0025「产物清单摘要」把签字绑在内容指纹上是同一个道理；
--   * 运行状态只存"外生意愿"两位（paused／cancelled），其余从步骤现算。
--     v0.86 修过的两个真 bug 都是"两套状态各自维护"造成的，这里不重犯。

CREATE TABLE agent_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 一次运行一定属于一个项目（工具的项目权限校验靠这一列），
    -- 但不一定属于某一集：整季盘点、跨集比对是项目级的。
    project_id      INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    episode_id      INTEGER REFERENCES episode(id) ON DELETE CASCADE,
    -- 人写的那句目标。原话存下来是为了事后能对上"他要的是什么、
    -- 平台自己理解成了什么"——这句话一旦被改写，运行就成了平台的自说自话
    goal            TEXT NOT NULL CHECK (length(trim(goal)) > 0),
    -- 任务场景：只是**引用** model_policy.scene_key，一列都不复制。
    -- 场景的策略改了不影响已发起的运行（运行冻的是下面那份快照）
    scene_key       TEXT NOT NULL DEFAULT '',
    -- 计划快照：算计划那次调用产出的步骤序列（工具码＋参数摘要）。
    -- 冻结在 run 上，理由与产线冻 `template_version` 一样——要能回答
    -- "当时打算做什么 vs 实际做了什么"，而不是每次读都重算一遍
    plan_json       TEXT NOT NULL DEFAULT '[]',
    -- 计划这一步的**结果**，不是 Job 的状态：作业成功不等于计划可用——
    -- 产物可能不是 JSON（本机 qwen3:8b 与云端 deepseek 都有过这种输出形状）。
    -- 少了这一列，"计划失败"就只能在派生状态里靠"读一行事件"来还原，
    -- 而那种派生既慢又容易与 finished_at 打架。
    plan_status     TEXT NOT NULL DEFAULT 'pending'
                        CHECK (plan_status IN ('pending','ready','failed')),
    -- 模型快照：走的是哪条轨（capability/backend/provider/model）＋凭什么走它。
    -- 存的是"选择"，不是"结论"：健康、驻留、在线与否一律现查不落库
    --（与 0040 的边界②同一条纪律）
    model_snapshot_json TEXT NOT NULL DEFAULT '{}',
    -- 算计划那次调用就是一条普通文本 Job（不开第二条队列，也不让 API 进程自己发 HTTP）。
    -- 计划没算出来之前 run 只有这一条腿在动；作业行随删项目 SET NULL，历史仍可查
    plan_job_id     INTEGER REFERENCES job(id) ON DELETE SET NULL,
    -- 审批有效期（秒）。说明书 §7 给 agent_approval 排的"有效期"落在这里：
    -- 审的是"接下来 24 小时内允许执行这一批参数"，不是一个永久授权
    approval_valid_seconds INTEGER NOT NULL DEFAULT 86400
                        CHECK (approval_valid_seconds BETWEEN 60 AND 604800),
    -- 只存外生意愿：暂停与取消是人按的，其余状态从步骤现算
    paused          INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    cancelled       INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1)),
    cancel_reason   TEXT NOT NULL DEFAULT '',
    -- 防连点两次起两条同样目标的任务。键由发起方生成（与产线同一做法：
    -- 幂等键的语义是"同一个请求重放"，不是"同一集只许跑一条"）
    idempotency_key TEXT UNIQUE,
    started_at      TEXT,
    finished_at     TEXT,
    created_by      INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_run_project ON agent_run(project_id, id DESC);
-- worker 的推进节拍每轮要捞"还没结束的运行"：按这两位筛，不用扫全表
CREATE INDEX IF NOT EXISTS idx_agent_run_active ON agent_run(cancelled, paused, id);


CREATE TABLE agent_step (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES agent_run(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    -- 工具码。CHECK 只挡空串，**白名单本身住在 `agent_tools.py`**：
    -- 那里才是"这个码有没有执行分支"的唯一事实，写在 SQL 里会变成第二份名单——
    -- 加一个工具要同时改两处、漏改一处就得到一个"点了没反应"的按钮（CR-010）
    tool_code    TEXT NOT NULL CHECK (length(trim(tool_code)) > 0),
    args_json    TEXT NOT NULL DEFAULT '{}',
    -- 参数指纹（规范化 JSON 的 sha256 前 16 位）。审批绑它，执行前再核一次它
    args_hash    TEXT NOT NULL,
    -- 风险档。四档的差别是"要不要人点头、点几次"，不是四个权限点：
    -- read 当场做完；write 要审批；paid 要审批＋云端逐次确认；formal 要审批＋事后验收
    risk         TEXT NOT NULL CHECK (risk IN ('read','write','paid','formal')),
    -- 步骤自己的进度。注意这**不是** Job 的状态：
    -- awaiting_approval／expired 这两档 Job 表里根本没有，
    -- 而 succeeded 是"关联的作业成功且结果验证过了"的合成结论
    status       TEXT NOT NULL CHECK (status IN (
                     'pending','awaiting_approval','approved','running',
                     'succeeded','failed','skipped','cancelled','expired')),
    -- 这一步产出的作业（如果有）。状态、成本、日志一律去 `job` 表现读，
    -- 这里只留这根指针——所以本表不可能与 Job 打架
    job_id       INTEGER REFERENCES job(id) ON DELETE SET NULL,
    -- 只存摘要与跳转需要的 id（读工具的结果条数、写工具建出来的业务对象 id）。
    -- 存业务对象 id 不存它的字段：对象改了就跟着变，抄一份就过期
    result_json  TEXT NOT NULL DEFAULT '{}',
    error_code   TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    retry_count  INTEGER NOT NULL DEFAULT 0,
    -- 审批到期时间。谁批的、批没批**不在这里**——那是 `review_item`
    -- （target_kind='agent_action'）那一条，界面只显示状态（不变量 3）
    approval_expires_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    -- 同一运行的同一步只有一行：计划重放、进程重启、请求重发都插不进来
    UNIQUE (run_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_agent_step_run ON agent_step(run_id, seq);
-- 推进节拍每轮找"该干活的那一步"：按状态筛，别扫全表
CREATE INDEX IF NOT EXISTS idx_agent_step_ready ON agent_step(status, run_id, seq);

-- 挂 Job 的外键列必须各自有索引（`test_schema.py` 那条守卫会逐条问）。
-- 这不是装饰：删一行 job 时 SQLite 要在子表里找出该置 NULL 的行，
-- 没索引就是整表扫，而删项目／删集都跑在写事务里——0036 实测过这个差是 522ms→71ms，
-- 半秒级持锁对局域网 2—5 人就是别人提交时那句"database is locked"。
CREATE INDEX IF NOT EXISTS idx_agent_run_plan_job ON agent_run(plan_job_id);
CREATE INDEX IF NOT EXISTS idx_agent_step_job ON agent_step(job_id);


CREATE TABLE agent_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 两条都可空：一条运行之外还有"发起被拒"这类没落成 run 的事件。
    -- 但它们**必须至少有一个**，那一句由仓储层判——CHECK 里引用别的表做不到
    run_id       INTEGER REFERENCES agent_run(id) ON DELETE CASCADE,
    step_id      INTEGER REFERENCES agent_step(id) ON DELETE SET NULL,
    event_type   TEXT NOT NULL CHECK (event_type IN (
                     'run_created','plan_requested','plan_ready','plan_failed',
                     'run_started','run_paused','run_resumed','run_cancelled',
                     'run_completed','run_failed',
                     'step_ready','step_started','step_succeeded','step_failed',
                     'step_skipped','step_retried','step_expired',
                     'approval_requested','approval_granted','approval_rejected',
                     'job_submitted','preflight_warning','tool_rejected')),
    -- 脱敏后的详情。四条不许进来：密钥值、请求头、完整正文、本机绝对路径以外的
    -- 环境细节——这张表会被接口整页拉走，也照样进导出
    payload_json TEXT NOT NULL DEFAULT '{}',
    actor_id     INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_event_run ON agent_event(run_id, id);
CREATE INDEX IF NOT EXISTS idx_agent_event_step ON agent_event(step_id, id);


-- ==================== 签字多一个可签对象 ====================
--
-- 说明书 §3.4 要"正式资产写入／剧本覆盖／生产提交按风险审批"，而今天
-- `review_item.target_kind` 只有 document／job 两档——审的都是**内容**
-- （这一版正文能不能用、这一条产物能不能进成片）。"允不允许发生一次动作"
-- 没有可签对象，于是只有两个坏选择：
--   甲 建 `agent_approval` 新表 ⇒ 平台上出现第二个"人点头"的落库处，
--      与不变量 3 直接撞；界面上也会有两个互不相认的"待审批"。
--   乙 扩这一列 ⇒ 审核中心本来就是签字的唯一入口，多态化也已经在
--      0025 真做过一次（document → document/job），同法、同风险面。
-- 裁定取 **乙**（审计文档《00-接入前现状审计》§6 丙的推荐案）。
--
-- 三档的语义分开写清楚，免得后人以为 agent_action 是"另一种产物审核"：
--   * document：target_id = document.id，base_version = 正文 sha256；
--   * job：target_id = job.id，base_version = 产物清单摘要；
--   * agent_action：target_id = agent_step.id，base_version = **那一步的 args_hash**。
--     绑参数而不是绑内容，是因为这一步审的是"这组参数允许执行"——
--     参数一改就失配，与"正文改一个字旧签字作废"是同一条道理。
-- 锚点用 `execute`（整步一次结论），不学产物那样逐件签：一次动作不可拆。
--
-- SQLite 改不了列的 CHECK，只能整表重建（0025 同一套写法）：
-- 建新表 → 搬数据 → 删旧表 → 改名 → 重建索引。migrate.py 在迁移期间关掉外键。
-- 现有两档的历史签字逐列点名搬运，一条不丢。

CREATE TABLE review_item_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    target_kind   TEXT NOT NULL DEFAULT 'document'
                  CHECK (target_kind IN ('document','job','agent_action')),
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

INSERT INTO review_item_new
    (id, project_id, target_kind, target_id, gate_id, base_version, anchor,
     decision, note, signed_by, signed_at)
SELECT id, project_id, target_kind, target_id, gate_id, base_version, anchor,
       decision, note, signed_by, signed_at
FROM review_item;

DROP TABLE review_item;
ALTER TABLE review_item_new RENAME TO review_item;

-- 索引原样重建（定义抄 0025，不加不减）：多态键的唯一性与队列取数仍是一回事
CREATE UNIQUE INDEX uq_review_item_target
    ON review_item(target_kind, target_id, base_version, anchor);
CREATE INDEX idx_review_item_project
    ON review_item(project_id, target_kind, id DESC);

-- 没有种子行：凭空 INSERT 一条运行或一步等于替人决定他要 Agent 干什么。
-- 第一行由人在「智能体中心」发起，工具白名单在代码里（agent_tools.py）。
