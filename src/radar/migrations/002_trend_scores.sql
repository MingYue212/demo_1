ALTER TABLE repository_snapshots
    ADD COLUMN IF NOT EXISTS commit_count_30d INTEGER;

ALTER TABLE repository_snapshots
    ADD COLUMN IF NOT EXISTS release_count_30d INTEGER;

ALTER TABLE repository_snapshots
    ADD COLUMN IF NOT EXISTS contributor_count_approx INTEGER;

CREATE TABLE IF NOT EXISTS trend_scores (
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    score_date DATE NOT NULL,
    algorithm_version TEXT NOT NULL,
    total_score DOUBLE PRECISION,
    star_velocity_score DOUBLE PRECISION,
    acceleration_score DOUBLE PRECISION,
    activity_score DOUBLE PRECISION,
    freshness_score DOUBLE PRECISION,
    quality_score DOUBLE PRECISION,
    reasons_json JSONB NOT NULL,
    calculated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (repository_id, score_date, algorithm_version)
);

CREATE INDEX IF NOT EXISTS idx_trend_scores_date_score
    ON trend_scores (score_date, total_score DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_repository_date
    ON repository_snapshots (repository_id, snapshot_date DESC);
