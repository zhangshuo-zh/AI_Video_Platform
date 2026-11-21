CREATE TABLE asset_candidate_review (
    asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    artifact_index INTEGER NOT NULL CHECK (artifact_index BETWEEN 0 AND 63),
    state TEXT NOT NULL CHECK (state IN ('favorite', 'archived')),
    PRIMARY KEY (asset_id, job_id, artifact_index)
);

CREATE TABLE asset_candidate_import (
    asset_id INTEGER NOT NULL REFERENCES asset(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    artifact_index INTEGER NOT NULL,
    variant_key INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (asset_id, job_id, artifact_index, variant_key)
);

CREATE INDEX idx_asset_candidate_review_job ON asset_candidate_review(job_id);
CREATE INDEX idx_asset_candidate_import_job ON asset_candidate_import(job_id);
