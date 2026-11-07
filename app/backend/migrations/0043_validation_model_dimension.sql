-- 0043：验证记录的"档"升为一等维度——换档不背书从读点补丁变成表的形状
--
-- 背景（2026-11-07 设计审查 P0-1）：0022 重建后的索引是
-- (capability, backend, provider)——验证只按**槽位**聚合，"验的是哪一档"
-- （`model` 列）只是普通列，于是"记录里的 model ＝ 槽位当前的 model"这条判断
-- 被写在每个读点上：`model_center._slot_model_matches()` 一份、
-- `services._service_engine_gate()` 一份。漏一个读点，就会出现
-- "1.3B 验过、5B 也算验过"——那是《使用说明书》§4.5 明令禁止的事。
--
-- 这次不改任何列、不改任何 CHECK：只把索引升维，让"按档取最新"成为
-- 一条索引能答的查询。表重建的步骤与 0022 相同（SQLite 的索引可以
-- DROP/CREATE，其实不必重建表——只动索引，搬数据那三步都省了）。
--
-- 0043 只做三件事：
-- 1. 旧索引 `idx_validation_slot` 换成带 `model` 的版本。读语义收窄：
--    "这一槽位这一档的验证史"，而不是"这一槽位所有档混在一起的验证史"。
-- 2. `latest_matrix()`（repositories/validation.py）的窗口函数从
--    PARTITION BY (capability, backend, provider, kind) 升为
--    PARTITION BY (capability, backend, provider, model, kind)——
--    同一 provider 换档后，旧档的最新记录仍然查得到（历史不丢），
--    但不再替新档说话；新档没验过就是没验过。
-- 3. 能力轨（`validations.summary_for`）与模型仓库（`model_center`）从此
--    读同一份"按档"的结论，两屏不再各说各话。
--
-- 历史行的 `model` 有空串（旧 env_check 可能没记档）：空串当作独立的一组，
-- 只会被"槽位当前也没填 model"的配置匹配到，不会错误背书任何具体档。

DROP INDEX IF EXISTS idx_validation_slot;
CREATE INDEX idx_validation_slot_model
    ON model_validation(capability, backend, provider, model, created_at DESC);
