"""HTTP API and Strategy Lab.

Two rules shape every response here:

* A point estimate never travels without its interval or its sample size.
* Any number that needed hindsight is labelled as such in the payload, so a
  client cannot render it as an achievable result by accident.
"""

from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from coinfinder import db, diagnostics, repo
from coinfinder.api.grid import default_grid
from coinfinder.backtest.costs import CostModel
from coinfinder.backtest.engine import FilterSpec, run, search, to_frame
from coinfinder.backtest.exits import DEFAULT_MODELS, by_name
from coinfinder.chains import ALL_CHAINS, get_chain
from coinfinder.config import get_settings
from coinfinder.logging_setup import setup_logging

log = structlog.get_logger(__name__)
WEB_DIR = pathlib.Path(__file__).resolve().parents[3] / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    try:
        await db.init_pool(max_size=6)
        await db.migrate()
    except Exception as exc:
        # Start anyway. A running app that explains "database unreachable" is
        # far more useful to a non-technical operator than a restart loop that
        # explains nothing - and /api/diagnostics is designed to say exactly
        # which setting is wrong.
        log.error("api.started_degraded", error=str(exc)[:200])
    else:
        log.info("api.started")
    yield
    await db.close_pool()


app = FastAPI(title="coin-finder", version="0.1.0", lifespan=lifespan)


class BacktestRequest(BaseModel):
    window_days: int = Field(default=30, ge=1, le=365)
    chains: list[str] = Field(default_factory=list)
    min_clusters: int | None = Field(default=None, ge=1, le=50)
    min_mcap_usd: float | None = Field(default=None, ge=0)
    max_mcap_usd: float | None = Field(default=None, ge=0)
    min_liquidity_usd: float | None = Field(default=None, ge=0)
    max_age_minutes: int | None = Field(default=None, ge=0)
    safe_only: bool = False
    exit_model: str = "ladder"
    trade_size_usd: float = Field(default=100.0, gt=0, le=1_000_000)
    dex_fee_bps: int = Field(default=30, ge=0, le=1000)
    gas_usd_per_swap: float = Field(default=0.05, ge=0, le=100)

    def to_spec(self) -> FilterSpec:
        chain_ids = tuple(get_chain(key).chain_id for key in self.chains if key in ALL_CHAINS)
        return FilterSpec(
            chains=chain_ids or None,
            min_clusters=self.min_clusters,
            min_mcap_usd=self.min_mcap_usd,
            max_mcap_usd=self.max_mcap_usd,
            min_liquidity_usd=self.min_liquidity_usd,
            max_age_minutes=self.max_age_minutes,
            safety_verdicts=("safe", "caution") if self.safe_only else None,
        )

    def to_cost(self) -> CostModel:
        return CostModel(dex_fee_bps=self.dex_fee_bps, gas_usd_per_swap=self.gas_usd_per_swap)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness, not correctness.

    Deliberately returns 200 even when the database is unreachable. The
    platform restarts a service whose health check fails, and that restart
    loop would stop the operator ever seeing the diagnostics page naming the
    setting they need to fix. Real state lives at /api/diagnostics and in the
    dashboard's status card, both of which say so loudly.
    """
    try:
        await db.fetchval("SELECT 1")
    except Exception as exc:
        return {
            "status": "degraded",
            "database": "unreachable",
            "detail": str(exc)[:200],
            "see": "/api/diagnostics",
            "time": datetime.now(UTC).isoformat(),
        }
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


@app.get("/api/meta")
async def meta() -> dict[str, Any]:
    """Everything a client needs to build its own controls."""
    return {
        "chains": [
            {
                "key": chain.key,
                "name": chain.name,
                "chain_id": chain.chain_id,
                "native": chain.native_symbol,
                "risk_data_available": chain.risk_data_available,
            }
            for chain in ALL_CHAINS.values()
        ],
        "exit_models": [
            {"name": m.name, "uses_look_ahead": m.uses_look_ahead} for m in DEFAULT_MODELS
        ],
    }


@app.get("/api/diagnostics")
async def diagnostics_endpoint(
    network: bool = Query(default=True, description="Also probe RPC and DexScreener"),
) -> dict[str, Any]:
    """Plain-language system status.

    This is the page a non-technical operator opens instead of reading logs,
    so it answers "is it broken, what is it doing, when do signals start"
    rather than dumping counters.
    """
    report = await diagnostics.run(include_network=network)
    return report.to_dict()


@app.get("/api/stats")
async def stats() -> dict[str, Any]:
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v)
        for k, v in (await repo.system_stats()).items()
    }


@app.get("/api/signals")
async def signals(
    days: int = Query(default=7, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    rows = await repo.signals_for_backtest(days)
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return {
        "count": len(rows),
        "signals": [
            {
                "id": r["id"],
                "chain_id": r["chain_id"],
                "token": r["token"],
                "symbol": r["symbol"],
                "ts": r["ts"].isoformat(),
                "clusters": r["distinct_clusters"],
                "mcap_usd": _f(r["snap_mcap_usd"]),
                "liquidity_usd": _f(r["snap_liquidity_usd"]),
                "age_minutes": r["snap_age_minutes"],
                "safety": r["safety_verdict"],
                "quality_score": r["quality_score"],
                "peak_multiple": r["peak_multiple"],
                "current_multiple": r["current_multiple"],
                "is_dead": r["is_dead"],
            }
            for r in rows[:limit]
        ],
    }


@app.post("/api/backtest")
async def backtest(request: BacktestRequest) -> dict[str, Any]:
    try:
        model = by_name(request.exit_model)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = await repo.signals_for_backtest(request.window_days)
    split = datetime.now(UTC) - timedelta(days=max(1, request.window_days // 3))
    result = run(
        rows,
        spec=request.to_spec(),
        exit_model=model,
        size_usd=request.trade_size_usd,
        cost=request.to_cost(),
        split_at=split,
    )
    return result.to_dict()


@app.get("/api/backtest/compare")
async def compare(
    window_days: int = Query(default=30, ge=1, le=365),
    trade_size_usd: float = Query(default=100.0, gt=0),
) -> dict[str, Any]:
    """Every exit model over the same signals.

    This is the comparison the reference product does not show: one headline
    number is chosen from this table, and which one you pick changes the
    answer more than any filter does.
    """
    rows = await repo.signals_for_backtest(window_days)
    frame = to_frame(rows)
    cost = CostModel()
    return {
        "window_days": window_days,
        "trade_size_usd": trade_size_usd,
        "results": [
            run(
                frame, spec=FilterSpec(), exit_model=model, size_usd=trade_size_usd, cost=cost
            ).to_dict()
            for model in DEFAULT_MODELS
        ],
    }


@app.get("/api/strategies/top")
async def top_strategies(
    window_days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=10, ge=1, le=50),
    min_signals: int = Query(default=25, ge=1),
    trade_size_usd: float = Query(default=100.0, gt=0),
) -> dict[str, Any]:
    rows = await repo.signals_for_backtest(window_days)
    split = datetime.now(UTC) - timedelta(days=max(1, window_days // 3))
    results = search(
        rows,
        specs=default_grid(),
        size_usd=trade_size_usd,
        min_signals=min_signals,
        split_at=split,
    )
    return {
        "window_days": window_days,
        "combinations_tested": len(default_grid())
        * len([m for m in DEFAULT_MODELS if not m.uses_look_ahead]),
        "note": (
            "Ranked on realistic exits only. The out_of_sample block is the "
            "column that matters: a combination that leads in-sample and "
            "collapses out-of-sample is fitted to the past, not predictive."
        ),
        "strategies": [r.to_dict() for r in results[:limit]],
    }


@app.get("/api/wallets/top")
async def top_wallets(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for chain in ALL_CHAINS.values():
        wallets = await repo.top_wallets(chain.chain_id, limit)
        if wallets:
            out[chain.key] = [
                {
                    "wallet": w["wallet"],
                    "score": w["score"],
                    "win_rate": w["win_rate"],
                    "median_multiple": w["median_multiple"],
                    "realized_pnl_usd": _f(w["realized_pnl_usd"]),
                    "closed_trades": w["closed_trades"],
                }
                for w in wallets
            ]
    return {"chains": out}


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")


def serve() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "coinfinder.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


if __name__ == "__main__":
    serve()
