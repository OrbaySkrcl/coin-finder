-- Per-user position sizing.
--
-- Execution cost is a function of position size, so an alert that prints the
-- round-trip cost at a fixed $100 tells a $10 trader the wrong number - and at
-- these sizes the cost is dominated by gas, which is per-transaction and
-- therefore hurts small positions most. Both settings below let the bot answer
-- "what does this trade cost *me*".

ALTER TABLE user_filters
    ADD COLUMN IF NOT EXISTS trade_size_usd NUMERIC(38, 2) NOT NULL DEFAULT 100,
    -- Suppress signals whose round-trip cost at this user's size exceeds this
    -- percentage. NULL disables the filter.
    ADD COLUMN IF NOT EXISTS max_cost_pct DOUBLE PRECISION;
