-- 0040：引擎实例——"这一档引擎在哪台机器的哪个端点"
--
-- 为什么要这张表：Draw Things 与 ComfyUI 是**常驻服务**，不是每次起的子进程。
-- 平台现有的执行形状只有两种（`runners.build()` 分发给本机子进程，或
-- `cloud_run.py` 给供应商发 HTTP），两者都不携带"端点"这个概念，所以接本机
-- 服务必须先有一个存端点的地方。CR-ENGINE-001 v1.1 的 W-B 就是这一步。
--
-- 三条边界，改这张表之前先读：
--
-- 1. **只存配置，不存结论**。这张表里故意没有 `health_status`、`detected_version`、
--    `last_probed_at`、`last_error_*` 这几列。平台的既有纪律是"存在性由 runtime.py
--    现查，不落库"（0035 对 `artifact_path` 的注释原话），健康每次由
--    `adapters.health()` 算；探测留痕已经有去处——`model_validation` 的 `env_check`
--    事件。把结论存成列就会长出两份答案，而"库里写在线、探测说文件不在"这种
--    漂移是最难查的一类 bug。
-- 2. **实例不替代规格**。`engine_code` 必须能在适配器清单（`adapters.py`）里找到，
--    校验住在路由层（与 `POST /api/models` 比 `adapters.find()` 同一做法）。
--    这张表回答"在哪"，不回答"能不能跑、能跑什么参数"。
-- 3. **密钥不进表**。`shared_secret_env` 存的是**变量名**，值永远只从进程环境读
--    （与 `provider_config.key_env` 同一条纪律）。`endpoint` 也不许带
--    user:pass@ 或查询串——那等于把凭据塞进一个会进导出与日志的字段。
--
-- 一个引擎允许多个实例（比如局域网里两台 ComfyUI），但第一阶段是否真需要多实例
-- 由实际需要决定，界面与接口都不必为此先造抽象。
--
-- 没有种子行：凭空 INSERT 一行"localhost:7860"等于替用户决定他的应用装在哪个端口，
-- 而且那行会在没人确认的情况下出现在生产候选里。第一行由人通过界面/接口加。

CREATE TABLE engine_instance (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    engine_code        TEXT NOT NULL CHECK (length(trim(engine_code)) > 0),
    display_name       TEXT NOT NULL DEFAULT '',
    -- 连接方式是有限枚举，由后端校验；界面不给自由文本，
    -- 否则"地址或可执行入口"这一格会变成任意命令的注入口（CR-001 明令禁止）。
    connection_type    TEXT NOT NULL DEFAULT 'local_http'
                           CHECK (connection_type IN ('local_http', 'grpc', 'cli')),
    endpoint           TEXT NOT NULL DEFAULT '',
    shared_secret_env  TEXT NOT NULL DEFAULT '',
    enabled            INTEGER NOT NULL DEFAULT 1,
    note               TEXT NOT NULL DEFAULT '',
    created_by         INTEGER REFERENCES app_user(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    -- 同一端点登记两次没有信息量，只会让"现在用的是哪一个"变成猜。
    UNIQUE (engine_code, endpoint)
);

CREATE INDEX IF NOT EXISTS idx_engine_instance_code
    ON engine_instance(engine_code, enabled);
