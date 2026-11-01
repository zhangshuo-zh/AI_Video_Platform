-- 0014：Provider 能力清单（W06）
--
-- 两列是本轮新增的配置项：
--   key_env  这个 provider 的密钥放在哪个环境变量里。留空＝按 FV_KEY_<PROVIDER>
--            推；但现实里已经有 DEEPSEEK_API_KEY 这种既有名字，不能强制改名。
--            注意这里存的是**变量名**，不是密钥值——值永远只从进程环境读。
--   notes    给这条槽位写备注（为什么选它、价格从哪来、哪天核过）。
--
-- 允许参数与健康状态不进库：前者由 adapter 清单按 provider+model 现算，
-- 后者是每次打开页面现查的实时结果。存下来就会变成"上次检查时是好的"。
ALTER TABLE provider_config ADD COLUMN key_env TEXT NOT NULL DEFAULT '';
ALTER TABLE provider_config ADD COLUMN notes TEXT NOT NULL DEFAULT '';

-- 默认轨按规格 01-4.8.2 归位：图像、视频、TTS、音乐默认本地，文本只有云端
-- 一条可用路线。v0.30 起这四类的默认一直挂在云端上，等于"配了云端就默认走云端"，
-- 与"云端视频必须逐镜头显式选择"正面冲突。
UPDATE provider_config
   SET is_default = CASE backend WHEN 'local' THEN 1 ELSE 0 END,
       updated_at = datetime('now','localtime')
 WHERE capability IN ('image', 'video', 'tts', 'music');
