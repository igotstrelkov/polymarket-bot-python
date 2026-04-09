CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,           -- 'BUY' | 'SELL'
    price NUMERIC(10,4) NOT NULL,
    size NUMERIC(14,4) NOT NULL,
    strategy TEXT NOT NULL,       -- 'A' | 'B' | 'C'
    time_in_force TEXT NOT NULL,  -- 'GTC' | 'GTD'
    post_only BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL,         -- 'SUBMITTED'|'CONFIRMED'|'CANCELLED'|'REJECTED'
    fee_rate_bps INTEGER,
    expiration BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fills (
    id BIGSERIAL PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price NUMERIC(10,4) NOT NULL,
    size NUMERIC(14,4) NOT NULL,
    strategy TEXT NOT NULL,
    maker_taker TEXT NOT NULL,    -- 'MAKER' | 'TAKER' — from User channel, not inferred
    simulated BOOLEAN NOT NULL DEFAULT FALSE,
    mid_at_fill NUMERIC(10,4),
    mid_at_30s NUMERIC(10,4),
    markout_30s NUMERIC(10,4),    -- positive = adverse, negative = favourable
    fill_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size NUMERIC(14,4) NOT NULL,
    avg_price NUMERIC(10,4) NOT NULL,
    realized_pnl NUMERIC(14,4) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rewards (
    id BIGSERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    date DATE NOT NULL,
    rewards_earned NUMERIC(10,4),
    rebates_earned NUMERIC(10,4),
    scoring_status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market_id, date)
);
