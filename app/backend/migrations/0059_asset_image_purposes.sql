ALTER TABLE asset ADD COLUMN image_purposes TEXT NOT NULL DEFAULT '{}';
ALTER TABLE asset_variant ADD COLUMN image_purposes TEXT NOT NULL DEFAULT '{}';
