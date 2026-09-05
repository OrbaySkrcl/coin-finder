# coin-finder

Smart-money signal engine and an honest backtesting lab for Base, Robinhood
Chain and BNB Chain.

It watches wallets with a demonstrated track record, alerts when several
*independent* ones buy the same token, and lets you replay any filter against
real history — net of the fees, price impact and gas that decide whether a
strategy actually made money.

---

## Why this exists

Signal bots in this space publish a number like *"1.54× median multiple,
+82.5% ROI"*. Three things are usually true of that number at once:

| Problem | What it does | What we do |
|---|---|---|
| **Look-ahead** | "Exit at 50% of the peak" needs to know the peak. You can only know it afterwards. | Every exit rule declares whether it needs hindsight. Hindsight rules are shown as a ceiling and are excluded from strategy ranking. |
| **No execution costs** | A quoted multiple ignores the swap fee, the price impact of trading into a thin pool, token taxes and gas. | Costs are derived from constant-product AMM maths and applied to every result. |
| **Survivorship** | Rugged tokens drop off the price API and quietly leave the sample. | A delisted token is recorded as dead and carried at −100%. |

The included demonstration makes the gap concrete. Calibrated to a real
published distribution, the hindsight exit reproduces the advertised
**+82.5% ROI** (we get +80.8%) — while realistic exits on the *same signals*
range from −46% to +80%, and a $2,000 position turns the best of them
negative.

That last point deserves its own table. In a $40,778 pool:

| Position size | Round-trip cost | Gross multiple needed to break even |
|---|---|---|
| $50 | 1.16% | 1.012× |
| $100 | 1.61% | 1.016× |
| $500 | 5.26% | 1.056× |
| $1,000 | 9.50% | 1.108× |
| $5,000 | 33.36% | **1.562×** |

A "1.54× median" strategy is a **losing** strategy at $5,000 per trade. No
filter fixes that; only position sizing does. Every alert therefore prints the
round-trip cost at your own size.

---

## How it works

```
   public RPC ─┐
               ├─▶ ingest ──▶ scoring ──▶ signal engine ──┬─▶ Telegram bot
 DexScreener ──┘   (trades)   (wallets)   (confluence)    └─▶ API + Strategy Lab
                                                              ▲
                              outcome tracker ────────────────┘
```

### Wallet-centric indexing

Indexing every swap on every pool is impossible on free RPC quotas. But ERC20
`Transfer` logs index both `from` and `to`, and `eth_getLogs` accepts a list of
values per topic position:

```
topics = [Transfer, null,     [w1, w2, … wN]]   → every token these wallets received
topics = [Transfer, [w1…wN],  null           ]   → every token they sent
```

One query covers every token movement of every tracked wallet. Cost scales with
how many wallets you follow, not with how busy the chain is. This single choice
is what makes the system viable without a paid data provider.

### Independent conviction, not address count

"Five smart wallets bought this" means nothing if one person runs all five —
and manufacturing that is the cheapest way to fake a signal. Wallets that
repeatedly buy the same tokens seconds apart are unioned into a cluster, and
conviction is counted in clusters. Alerts show both numbers, so the collapse is
visible: *"5 wallets → 3 independent"*.

### A probability, not a star rating

Quality is `P(reaches 2x)`, which can be checked against what happened. It
ships as an explicit prior marked `is_fitted=False`, and is replaced by a model
fitted on real outcomes once 400 signals have resolved. Nothing presents
prior-based numbers as measured performance.

### Wallet scoring

Wallets are ranked on completed round trips only — marking open positions to
market would let a wallet look brilliant for holding a token it can never sell.
Win rate is shrunk toward a Beta(2,3) prior so a 3-for-3 record cannot top the
table, performance decays with a 30-day half-life, and PnL saturates so one
lucky 500× cannot own the leaderboard. Snipers and MEV bots are excluded on
behaviour: nobody can follow a trade that closes in four seconds.

---

## Running it

### Locally

```bash
uv sync --all-groups
docker compose up -d          # PostgreSQL + Redis
cp .env.example .env          # set DATABASE_URL and TELEGRAM_BOT_TOKEN
uv run python -m coinfinder   # everything, dashboard on :8000
```

`RUN_COMPONENTS=api` (or `worker`, `bot`) runs one part at a time.

Want to see the dashboard before any real signals exist?

```bash
uv run python scripts/seed_demo.py --yes --signals 1500 --days 75
```

The seeder writes clearly synthetic data and refuses to run without `--yes`.

### On Railway

One service runs everything. Deploy this repository, add the **PostgreSQL**
plugin, and set two variables:

```
DATABASE_URL       = ${{Postgres.DATABASE_URL}}
TELEGRAM_BOT_TOKEN = <token from @BotFather>
```

Migrations apply on startup, so the start command is just `python -m coinfinder`.

Splitting the parts across separate services is better at scale and needs no
code change — set `RUN_COMPONENTS` to `api`, `worker` or `bot` per service.

**Guides in Turkish, written for someone who does not code:**

* [`docs/BASLANGIC.md`](docs/BASLANGIC.md) — setup, click by click, no terminal
* [`docs/KULLANIM.md`](docs/KULLANIM.md) — reading alerts, filters, Strategy Lab
* [`docs/KURULUM.md`](docs/KURULUM.md) — operations reference and tuning

---

## Operating it without reading logs

The person running this does not code, so every operational question is
answered in the two places they already look:

* **`/durum` in Telegram** and the **status card** at the top of the dashboard
  both render the same diagnosis: what is connected, what is not, which setup
  step is currently running, and when to expect the first signals.
* If the database is unreachable the app still starts and serves that
  diagnosis rather than crash-looping, because a restart loop tells a
  non-technical operator nothing at all. `/health` therefore reports liveness,
  not correctness — real state lives at `/api/diagnostics`.

---

## Free-tier operation

Everything runs on free data sources: public RPC endpoints, DexScreener and
GeckoTerminal. To stay inside their limits:

* RPC calls rotate across several endpoints, each with its own token bucket and
  exponential cooldown on 429s. A provider that rejects a block range triggers
  a halving retry rather than a failover.
* Requests are batched wherever JSON-RPC allows it.
* Cadences are matched to cost: wallets are watched every 12 seconds,
  re-scoring runs every 6 hours.

Moving to a paid provider needs no code change — set `BASE_RPC_URLS`,
`ROBINHOOD_RPC_URLS` or `BSC_RPC_URLS` and the pool uses them.

---

## Chain support

| Chain | ID | Notes |
|---|---|---|
| Base | 8453 | Uniswap V2/V3/V4, Aerodrome |
| Robinhood Chain | 4663 | Arbitrum Orbit L2. **No third-party risk tooling covers it**, so it can never be reported as "safe" — alerts carry an explicit DYOR warning. |
| BNB Chain | 56 | PancakeSwap |

Adding a chain means adding a `Chain` entry in `src/coinfinder/chains.py`. No
ingestion code changes.

---

## Testing

```bash
uv run pytest -q          # 194 tests
uv run ruff check src tests scripts
uv run mypy
```

Tests that need PostgreSQL skip themselves when none is reachable, so the suite
runs anywhere. Point `COINFINDER_TEST_DSN` at a throwaway database to include
them — the integration suite truncates every table.

---

## What this is not

This is a data and analysis tool, not trading advice, and it does not execute
trades. There is no custody of user funds anywhere in the codebase.

Most tokens in this market go to zero. The outcome spread on every backtest
shows exactly how often — read it before the headline number.
