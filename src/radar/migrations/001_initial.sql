-- 仓库基础元数据：按 GitHub repository ID 保持实体稳定。
CREATE TABLE IF NOT EXISTS repositories (
    id BIGINT PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    description TEXT,
    primary_language TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    pushed_at TIMESTAMPTZ,
    archived BOOLEAN NOT NULL,
    is_fork BOOLEAN NOT NULL,
    discovered_at TIMESTAMPTZ NOT NULL,
    discovery_source TEXT NOT NULL
);

-- 每个仓库每天一条快照，用复合主键保证重复采集幂等。
CREATE TABLE IF NOT EXISTS repository_snapshots (
    repository_id BIGINT NOT NULL REFERENCES repositories(id),
    snapshot_date DATE NOT NULL,
    stars INTEGER NOT NULL CHECK (stars >= 0),
    forks INTEGER NOT NULL CHECK (forks >= 0),
    open_issues INTEGER NOT NULL CHECK (open_issues >= 0),
    watchers INTEGER NOT NULL CHECK (watchers >= 0),
    collected_at TIMESTAMPTZ NOT NULL,
    raw_payload_hash TEXT NOT NULL,
    PRIMARY KEY (repository_id, snapshot_date)
);
