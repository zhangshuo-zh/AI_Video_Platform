-- 0042：引擎实例的探测留痕——"这台上周还连不连得上"

-- 为什么另开一张表而不是塞进 `model_validation`（0021）：
-- 那张表的索引形状是 (capability, backend, provider)，一个 Slot 的验证史；
-- 而实例是"某一档引擎在某个端点"这一件事，与能力轴不是同一维度——
-- 同一个 draw_things 可以有两条实例（本机 7860 与局域网另一台 7860），
-- 塞进 model_validation 要么伪造一个不存在的 provider，要么把端点编码成字符串
-- 塞进 provider 列，两种都是给未来的自己下套。这一点 0040 的表头与
-- 《系统设计说明书》§16 第 26 条早就如实登记过："要留痕得先扩形状，是独立改动"。

-- **与 `model_validation`（0021／0045）的分工**（2026-11-07 补写这一句）：
-- 两张表都只追加，但主体不同——那边的主体是「能力×后端×provider×档」的**验证凭据**
-- （这一档凭什么被认为能用），这边的主体是「某条实例在某个时刻」的**探测事件**
-- （这一个端点上一次被问是什么时候）。所以两张表的 `state` 枚举不是"两套说法"，
-- 而是**窄表枚举是宽表枚举的真子集**：这里只可能出现 ok／missing／unknown
-- （`engines.EngineStatus` 就只产这三档），而 `model_validation` 认六档
-- （多了 unconfigured／unregistered／failed，那三档说的是"配置/清单/人判不通过"，
-- 一次 HTTP 探测答不出这些）。谁的结论住哪张表，看主体是谁，不看谁更全。
--
-- 这条边界不用统一枚举表达，是因为统一的那个"公共事件形状"会逼着这张表
-- 接受它永远产不出的三个值——那等于给未来的读代码的人一个假承诺。

-- 这张表只回答一件事：**某条实例在某个时刻，探了一次，结果是什么**。
-- 三条纪律：
--
-- 1. **只有显式动作才写**。`POST /api/engines/{id}/probe`（人按了「探测」）与
--    建实例时那一次顺手探测会留痕；`GET /api/engines` 是读路径，一次都不写。
--    读路径只**读**最近一条，用来把"上次探测是什么时候、结论如何"摆出来。
--    这条与 0040 那条"健康不落库"不冲突：0040 不许落的是**当前状态**（那会有
--    两份答案），这里落的是**历史事件**（只追加、不参与任何判断）——
--    现在的状态仍然由 `engines.probe()` 现查，这一张表永远不被读成"它是好的"。
-- 2. **只追加，不改不删**（随实例 CASCADE 除外）。改配置不能把历史洗掉，
--    与 `model_validation` 同一条纪律。
-- 3. **不存密钥值**。只存端点与结论；`shared_secret_env` 是变量名，它的值
--    从来不进任何表、任何响应。

CREATE TABLE IF NOT EXISTS engine_probe_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id   INTEGER REFERENCES engine_instance(id) ON DELETE CASCADE,
    -- 冗余存 engine_code 与 endpoint：实例被撤之后，"当时探的是哪一台"
    -- 仍然答得出来（instance_id 会变 NULL，但这一行还在）。与 model_validation
    -- 存 provider/model 快照同一个理由。
    engine_code   TEXT NOT NULL DEFAULT '',
    endpoint      TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL CHECK (state IN ('ok', 'missing', 'unknown')),
    engine_model  TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',
    latency_ms    INTEGER,
    -- manual＝人按了「探测」；create＝建实例时顺手探的那一次。
    -- 分开是因为读起来意思不同：前者是"人去问过"，后者是"建完顺手看一眼"。
    source        TEXT NOT NULL DEFAULT 'manual'
                      CHECK (source IN ('manual', 'create')),
    created_by    INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 这一条索引只服务一个查询："这条实例最近 N 次探测"。
-- 不建 engine_code 上的索引：跨实例按 code 横着查不是现在的界面需求，
-- 真需要时再加——现在多一条索引就是多一份要维护的写入成本。
CREATE INDEX IF NOT EXISTS idx_engine_probe_instance
    ON engine_probe_log(instance_id, id DESC);
