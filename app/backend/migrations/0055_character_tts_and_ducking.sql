-- 0055: M3 首尾帧参考位（shot.first_frame_image / last_frame_image）与人物专属 TTS 音色绑定字段
ALTER TABLE shot ADD COLUMN first_frame_image TEXT NOT NULL DEFAULT '';
ALTER TABLE shot ADD COLUMN last_frame_image  TEXT NOT NULL DEFAULT '';

ALTER TABLE asset_character_profile ADD COLUMN tts_voice TEXT NOT NULL DEFAULT '';
ALTER TABLE asset_character_profile ADD COLUMN tts_rate  REAL NOT NULL DEFAULT 1.0;
ALTER TABLE asset_character_profile ADD COLUMN tts_mood  TEXT NOT NULL DEFAULT '';
