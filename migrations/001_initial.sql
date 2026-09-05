-- coin-finder initial schema
-- Addresses are stored lower-case hex with 0x prefix.

CREATE TABLE IF NOT EXISTS tokens (
    chain_id        INTEGER      NOT NULL,
    address         TEXT         NOT NULL,
    symbol          TEXT,
    name            TEXT,
    decimals        SMALLINT     NOT NULL DEFAULT 18,
    pair_address    TEXT,
    dex_id          TEXT,
    deployer        TEXT,
    launched_at     TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    meta_updated_at TIMESTAMPTZ,
    is_quote_token  BOOLEAN      NOT NULL DEFAULT FALSE,
    PRIMARY KEY (chain_id, address)
);
CREATE INDEX IF NOT EXISTS tokens_launched_idx ON tokens (chain_id, launched_at DESC);

-- Latest known market state per token (upserted; cheap to read).
CREATE TABLE IF NOT EXISTS token_market (
    chain_id       INTEGER     NOT NULL,
    address        TEXT        NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    price_usd      NUMERIC(38, 18),
    mcap_usd       NUMERIC(38, 4),
    fdv_usd        NUMERIC(38, 4),
    liquidity_usd  NUMERIC(38, 4),
    volume_24h_usd NUMERIC(38, 4),
    buys_24h       INTEGER,
    sells_24h      INTEGER,
    is_delisted    BOOLEAN     NOT NULL DEFAULT FALSE,
    PRIMARY KEY (chain_id, address),
    FOREIGN KEY (chain_id, address) REFERENCES tokens (chain_id, address) ON DELETE CASCADE
);

-- Append-only price series. Backtests replay from here, never from live data.
CREATE TABLE IF NOT EXISTS price_history (
    chain_id      INTEGER     NOT NULL,
    address       TEXT        NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    price_usd     NUMERIC(38, 18) NOT NULL,
    mcap_usd      NUMERIC(38, 4),
    liquidity_usd NUMERIC(38, 4),
    PRIMARY KEY (chain_id, address, ts)
);
CREATE INDEX IF NOT EXISTS price_history_ts_idx ON price_history (ts DESC);

CREATE TABLE IF NOT EXISTS wallets (
    chain_id       INTEGER     NOT NULL,
    address        TEXT        NOT NULL,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ,
    is_contract    BOOLEAN     NOT NULL DEFAULT FALSE,
    is_excluded    BOOLEAN     NOT NULL DEFAULT FALSE,
    exclude_reason TEXT,
    funder         TEXT,
    watch_since    TIMESTAMPTZ,
    PRIMARY KEY (chain_id, address)
);
-- Partial index: the wallet watcher only ever scans currently-tracked wallets.
CREATE INDEX IF NOT EXISTS wallets_watched_idx
    ON wallets (chain_id, address) WHERE watch_since IS NOT NULL AND NOT is_excluded;

-- Normalised buy/sell events. One row per (tx, log) so replays are idempotent.
CREATE TABLE IF NOT EXISTS wallet_trades (
    chain_id      INTEGER     NOT NULL,
    tx_hash       TEXT        NOT NULL,
    log_index     INTEGER     NOT NULL,
    wallet        TEXT        NOT NULL,
    token         TEXT        NOT NULL,
    block_number  BIGINT      NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    side          TEXT        NOT NULL CHECK (side IN ('buy', 'sell')),
    token_amount  NUMERIC(78, 0) NOT NULL,
    usd_value     NUMERIC(38, 4),
    price_usd     NUMERIC(38, 18),
    PRIMARY KEY (chain_id, tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS wallet_trades_wallet_idx ON wallet_trades (chain_id, wallet, ts DESC);
CREATE INDEX IF NOT EXISTS wallet_trades_token_ts_idx ON wallet_trades (chain_id, token, ts DESC);

-- FIFO accounting state, one row per (wallet, token) lot-set.
CREATE TABLE IF NOT EXISTS wallet_positions (
    chain_id          INTEGER NOT NULL,
    wallet            TEXT    NOT NULL,
    token             TEXT    NOT NULL,
    qty_open          NUMERIC(78, 0) NOT NULL DEFAULT 0,
    cost_basis_usd    NUMERIC(38, 4) NOT NULL DEFAULT 0,
    realized_pnl_usd  NUMERIC(38, 4) NOT NULL DEFAULT 0,
    invested_usd      NUMERIC(38, 4) NOT NULL DEFAULT 0,
    proceeds_usd      NUMERIC(38, 4) NOT NULL DEFAULT 0,
    lots              JSONB   NOT NULL DEFAULT '[]'::jsonb,
    first_buy_at      TIMESTAMPTZ,
    last_activity_at  TIMESTAMPTZ,
    is_closed         BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (chain_id, wallet, token)
);

-- Sybil clusters: wallets sharing a funding source count as ONE conviction unit.
CREATE TABLE IF NOT EXISTS wallet_clusters (
    chain_id    INTEGER NOT NULL,
    wallet      TEXT    NOT NULL,
    cluster_id  TEXT    NOT NULL,
    reason      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, wallet)
);
CREATE INDEX IF NOT EXISTS wallet_clusters_cluster_idx ON wallet_clusters (chain_id, cluster_id);

CREATE TABLE IF NOT EXISTS wallet_scores (
    chain_id            INTEGER NOT NULL,
    wallet              TEXT    NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_days         INTEGER NOT NULL,
    closed_trades       INTEGER NOT NULL DEFAULT 0,
    wins                INTEGER NOT NULL DEFAULT 0,
    win_rate            DOUBLE PRECISION,
    median_multiple     DOUBLE PRECISION,
    realized_pnl_usd    NUMERIC(38, 4),
    avg_hold_minutes    DOUBLE PRECISION,
    distinct_tokens     INTEGER NOT NULL DEFAULT 0,
    score               DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_smart            BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (chain_id, wallet)
);
CREATE INDEX IF NOT EXISTS wallet_scores_rank_idx ON wallet_scores (chain_id, score DESC);

-- Signals are IMMUTABLE. Every field prefixed snap_ is the value known at
-- signal time; backtests may read nothing else, which is what keeps them
-- free of look-ahead bias.
CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    chain_id            INTEGER     NOT NULL,
    token               TEXT        NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    block_number        BIGINT,
    dedupe_key          TEXT        NOT NULL UNIQUE,
    distinct_wallets    INTEGER     NOT NULL,
    distinct_clusters   INTEGER     NOT NULL,
    wallets             JSONB       NOT NULL DEFAULT '[]'::jsonb,
    native_spent        NUMERIC(38, 18),
    usd_spent           NUMERIC(38, 4),
    snap_price_usd      NUMERIC(38, 18),
    snap_mcap_usd       NUMERIC(38, 4),
    snap_fdv_usd        NUMERIC(38, 4),
    snap_liquidity_usd  NUMERIC(38, 4),
    snap_age_minutes    INTEGER,
    snap_holders        INTEGER,
    snap_buy_tax_bps    INTEGER,
    snap_sell_tax_bps   INTEGER,
    snap_top10_pct      DOUBLE PRECISION,
    snap_lp_locked_pct  DOUBLE PRECISION,
    snap_volume_24h_usd NUMERIC(38, 4),
    safety_flags        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    safety_verdict      TEXT        NOT NULL DEFAULT 'unknown',
    quality_score       DOUBLE PRECISION,
    quality_p2x         DOUBLE PRECISION,
    alert_sent_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS signals_ts_idx ON signals (ts DESC);
CREATE INDEX IF NOT EXISTS signals_token_idx ON signals (chain_id, token, ts DESC);

-- Rolling outcome tracking, updated by a worker. Backtests read this.
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id        BIGINT      PRIMARY KEY REFERENCES signals (id) ON DELETE CASCADE,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_price_usd   NUMERIC(38, 18),
    last_mcap_usd    NUMERIC(38, 4),
    current_multiple DOUBLE PRECISION,
    peak_multiple    DOUBLE PRECISION,
    peak_at          TIMESTAMPTZ,
    drawdown_from_peak DOUBLE PRECISION,
    is_dead          BOOLEAN     NOT NULL DEFAULT FALSE,
    -- Multiple at fixed horizons; NULL until that horizon is reached.
    mult_15m  DOUBLE PRECISION,
    mult_1h   DOUBLE PRECISION,
    mult_4h   DOUBLE PRECISION,
    mult_24h  DOUBLE PRECISION,
    mult_7d   DOUBLE PRECISION
);

-- Telegram users.
CREATE TABLE IF NOT EXISTS users (
    telegram_id   BIGINT      PRIMARY KEY,
    username      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    trial_ends_at TIMESTAMPTZ,
    plan          TEXT        NOT NULL DEFAULT 'trial',
    paid_until    TIMESTAMPTZ,
    is_blocked    BOOLEAN     NOT NULL DEFAULT FALSE,
    alerts_paused BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS user_filters (
    telegram_id     BIGINT PRIMARY KEY REFERENCES users (telegram_id) ON DELETE CASCADE,
    chains          TEXT[]  NOT NULL DEFAULT ARRAY['base','robinhood','bsc'],
    min_clusters    INTEGER NOT NULL DEFAULT 3,
    min_mcap_usd    NUMERIC(38, 4),
    max_mcap_usd    NUMERIC(38, 4),
    min_liquidity_usd NUMERIC(38, 4) DEFAULT 5000,
    max_age_minutes INTEGER,
    require_safe    BOOLEAN NOT NULL DEFAULT TRUE,
    min_quality     DOUBLE PRECISION DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    telegram_id BIGINT      NOT NULL,
    signal_id   BIGINT      NOT NULL REFERENCES signals (id) ON DELETE CASCADE,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (telegram_id, signal_id)
);

-- Forward test: virtual positions opened on every signal by a fixed strategy.
CREATE TABLE IF NOT EXISTS paper_positions (
    id           BIGSERIAL PRIMARY KEY,
    strategy     TEXT        NOT NULL,
    signal_id    BIGINT      NOT NULL REFERENCES signals (id) ON DELETE CASCADE,
    opened_at    TIMESTAMPTZ NOT NULL,
    entry_price  NUMERIC(38, 18) NOT NULL,
    size_usd     NUMERIC(38, 4)  NOT NULL,
    entry_cost_usd NUMERIC(38, 4) NOT NULL DEFAULT 0,
    closed_at    TIMESTAMPTZ,
    exit_price   NUMERIC(38, 18),
    exit_reason  TEXT,
    pnl_usd      NUMERIC(38, 4),
    UNIQUE (strategy, signal_id)
);

-- Indexer checkpoints, one row per (chain, job).
CREATE TABLE IF NOT EXISTS ingest_checkpoints (
    chain_id     INTEGER NOT NULL,
    job          TEXT    NOT NULL,
    last_block   BIGINT  NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, job)
);
