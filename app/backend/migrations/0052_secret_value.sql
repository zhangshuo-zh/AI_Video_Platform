-- 0052：供应商密钥可以密文入库，管理员在网页上配（2026-11-11）
--
-- 背景与授权：用户在 01:05 提过"参数管理页面"，01:23 评估过"密文保存"，
-- 当时因为"无加密库 + KEK 死结"选了 (b2)（路径可配、密钥只走环境变量）。
-- 2026-11-11 03:26 用户重新确认：**实现 A**——新增 `cryptography` 依赖、
-- 用真 AES-256-GCM 加密，KEK 走环境变量 `PLATFORM_SECRET_KEK`（名字通用，
-- 不带具体模型名）。所以这张表带着明确授权进来。
--
-- **和 `runtime_path`（0051）分表而不是复用**：0051 的 CHECK 白名单刻意把密钥
-- 挡在外面（那是当时的选择），且两者语义不同——路径是明文可读配置，
-- 密钥是密文敏感值，列结构（nonce/密文/tag/指纹）也完全不同。合成一张表会让
-- "哪几行是密钥"要靠判断而非约束。
--
-- **存的是密文，不是明文。** 四个字段的关系：
--   * `ciphertext` = AES-256-GCM(KEK, nonce, 明文密钥)，含 GCM tag
--   * `nonce`      = 12 字节随机数，**每次写入重新生成**（绝不复用：GCM 的
--                    nonce 复用会直接泄漏明文异或值，是这套方案唯一的硬伤）
--   * `aad`        = 附加认证数据，绑定 `env_name`——防止把 A 变量的密文
--                    挪到 B 变量行上冒充（换行即解密失败）
--   * `fingerprint`= sha256(明文) 前 8 位，**为了让页面能显示"是哪一把"**
--                    而不必解密；与 `adapters.key_fingerprint` 同一算法，
--                    保证两处算出的指纹可比对。
--   * `length`     = 明文长度，同上为显示用（多个换行/少几位一眼看出）
--
-- **KEK 不在库里，只在环境变量。** 这是这套设计的安全边界：库被拷走但 KEK 没
-- 被拷走时，密文解不开。反过来若 KEK 也入库，加密就成了摆设（等于把钥匙和
-- 保险箱放一起）。`requirements.txt` 里那条注释写明了同一件事。
--
-- `updated_by` 不设外键：与 0044/0047/0051 同一纪律，删了账号这行还得站着。
--
-- 白名单钉死 8 个变量（与 `adapters.py` 里声明 `key_env` 的那批一一对应）。
-- 加新供应商时先在 adapters 加 `key_env`，再来这里加一行——四处（迁移 CHECK、
-- 仓储白名单、`_KEY_SHAPED` 特征检查、界面）都要加，少一处就是一道漏。

CREATE TABLE IF NOT EXISTS secret_value (
    env_name    TEXT PRIMARY KEY CHECK (env_name IN (
                  'DEEPSEEK_API_KEY',
                  'DASHSCOPE_API_KEY',
                  'FV_KEY_JIMENG',
                  'FV_KEY_KLING',
                  'FV_KEY_MINIMAX',
                  'FV_KEY_SEEDANCE',
                  'FV_KEY_VOLC',
                  'FV_KEY_WANX'
                )),
    -- 密文与它的 nonce。两列分开存是因为解密时 nonce 要原样喂回去；
    -- 拼成一段（如 `nonce||ct`）也能用，但读库时看不出哪段是什么，
    -- 排查"这行是不是坏了"要写代码解才看得出来。
    ciphertext  BLOB NOT NULL,
    nonce       BLOB NOT NULL,
    -- 绑定 env_name 的附加认证数据：改了行名就解不开，防止密文串门。
    aad         BLOB NOT NULL,
    -- 下面三个是**显示用**的冗余，都不足以还原明文（sha256 单向、长度只有位数）。
    -- 冗余的代价是"指纹可能与密文不一致"，所以每次写入都以明文为准重算，
    -- 且解密成功后再校验一次（见 `secrets_unlock()`）。
    fingerprint TEXT NOT NULL DEFAULT '',
    length      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT,
    updated_by  INTEGER,
    note        TEXT NOT NULL DEFAULT ''
);
