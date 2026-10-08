CREATE TABLE IF NOT EXISTS tiers (
    name       TEXT PRIMARY KEY,
    per_minute INT NOT NULL CHECK (per_minute > 0),
    burst      INT NOT NULL CHECK (burst > 0)
);

CREATE TABLE IF NOT EXISTS api_keys (
    api_key    TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    tier       TEXT NOT NULL REFERENCES tiers(name),
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_keys_owner ON api_keys (owner);

CREATE TABLE IF NOT EXISTS requests (
    id        BIGSERIAL PRIMARY KEY,
    api_key   TEXT NOT NULL,
    resource  TEXT NOT NULL,
    status    INT NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'sliding',
    at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS requests_key ON requests (api_key, id DESC);
