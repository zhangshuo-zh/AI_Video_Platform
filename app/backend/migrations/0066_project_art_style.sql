-- 项目级视觉画风锁定（2d / 25d / 3d），确保全剧集资产、抽卡与生图画风一致。
ALTER TABLE project ADD COLUMN art_style TEXT NOT NULL DEFAULT '2d';
