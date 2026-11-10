-- 0051：本地运行时路径可以从网页改（2026-11-11）
--
-- 为什么新开一张表而不是塞进现成的表：
--   * `config_change_log`（0044）只追加、不承载现值，它回答"怎么变成现在这样"；
--   * `provider_config`（0003）是每能力轨一行，回答"每个能力选哪一档模型"；
--   * `worker_limit`（0047）是并发上限，单例但语义完全不同。
-- 这三张都不能塞：一张表同时表达"选哪个模型"和"模型权重在哪个目录"，读的人
-- 得先知道哪几行是配置、哪几行是例外。与 0047 的判断同一条理由。
--
-- **这张表只存"路径类"参数，绝不含任何密钥值。**
-- `adapters.py` 文件头三条边界之第 3 条是"密钥只认变量名，值永远只从进程环境读"。
-- 密钥进库会让 `data/platform.db`（可备份、可拷贝、明文）成为全平台密钥的泄漏面，
-- 并且解密需要的主密钥最终仍得放环境变量——绕一圈没有减少任何一次部署改动。
-- 因此 `env_name` 的 CHECK 用白名单钉死：只允许这 9 个已知的路径/可执行文件变量，
-- 任何人想用这张表存 `DEEPSEEK_API_KEY` 会在写库时被约束直接拒掉。
--
-- 为什么存"覆盖值"而不是"最终值"：
--   环境变量仍是最高优先级（部署脚本、CI、`run.sh` 的推导都靠它）。
--   这一行是**人从网页上设的覆盖**，NULL 表示"没人在网页上设过，按环境变量或默认走"。
--   把两者混成一列，就分不清"环境里配的"和"网页上设的"——而这正是排查时最要紧的一步：
--   页面上要同时显示"当前进程实际生效值"与"库里已保存值"，两者不一致就说明
--   worker 还没重启。
--
-- `updated_by` 不设外键：删了账号之后这行还得 standalone 地站着，说明"最后是谁改的"。
-- 与 `config_change_log`（0044）和 `worker_limit`（0047）同一纪律。

CREATE TABLE IF NOT EXISTS runtime_path (
    -- 环境变量名就是主键：一个变量一行，没有第二条命名法。
    -- 白名单与 `adapters.py` / `runtime.py` 里实际读取的那批一一对应；
    -- 加新引擎时先在那两处加读取点，再来这里加一行。
    env_name    TEXT PRIMARY KEY CHECK (env_name IN (
                  'FASTVIDEO_FASTMETAL_HOME',
                  'FASTVIDEO_FASTMETAL_MODEL',
                  'FASTVIDEO_MLX_VIDEO_HOME',
                  'FASTVIDEO_MLX_VIDEO_MODEL',
                  'FASTVIDEO_MFLUX_HOME',
                  'FASTVIDEO_MFLUX_BIN',
                  'FASTVIDEO_VOXCPM_HOME',
                  'FASTVIDEO_ACE_STEP_HOME',
                  'FASTVIDEO_MLX_AUDIO_BIN',
                  'FASTVIDEO_RIFE_WEIGHTS'
                )),
    -- NULL = 没人在网页上设过（跟随环境变量/默认值）。
    -- 空串也视为"清除覆盖"，写入前由仓储层归一成 NULL——两种"没设"不该有两种表示。
    path        TEXT,
    updated_at  TEXT,
    updated_by  INTEGER,
    note        TEXT NOT NULL DEFAULT ''
);
