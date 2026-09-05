"""Runtime configuration. Every secret comes from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- infrastructure -------------------------------------------------
    database_url: str = Field(
        default="postgresql://coinfinder:coinfinder@localhost:5432/coinfinder"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    log_level: str = Field(default="INFO")
    environment: str = Field(default="development")

    # --- telegram -------------------------------------------------------
    telegram_bot_token: str = Field(default="")
    telegram_admin_ids: str = Field(default="")
    telegram_webhook_base: str = Field(default="")
    telegram_webhook_secret: str = Field(default="")

    # --- api ------------------------------------------------------------
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    public_base_url: str = Field(default="http://localhost:8000")

    # --- chains ---------------------------------------------------------
    enabled_chains: str = Field(default="base,robinhood,bsc")
    # Optional comma-separated overrides; when empty the built-in public
    # endpoint pool from chains.py is used.
    base_rpc_urls: str = Field(default="")
    robinhood_rpc_urls: str = Field(default="")
    bsc_rpc_urls: str = Field(default="")

    # --- ingestion tuning (free-tier friendly defaults) -----------------
    # How many wallet addresses to pack into one eth_getLogs topic filter.
    wallet_watch_batch: int = Field(default=120)
    # Blocks pulled per getLogs call. Public RPCs commonly cap this.
    log_range_blocks: int = Field(default=500)
    # Confirmations before a block is treated as final (reorg safety).
    reorg_confirmations: int = Field(default=12)
    poll_interval_seconds: float = Field(default=12.0)

    # --- signal engine --------------------------------------------------
    confluence_min_clusters: int = Field(default=3)
    confluence_window_minutes: int = Field(default=180)
    signal_cooldown_minutes: int = Field(default=360)
    min_liquidity_usd: float = Field(default=5_000.0)
    max_entry_mcap_usd: float = Field(default=5_000_000.0)

    # --- scoring --------------------------------------------------------
    score_window_days: int = Field(default=90)
    score_halflife_days: float = Field(default=30.0)
    smart_wallet_min_trades: int = Field(default=8)
    smart_wallet_top_n: int = Field(default=600)

    # --- backtest cost model --------------------------------------------
    default_trade_size_usd: float = Field(default=100.0)
    dex_fee_bps: int = Field(default=30)
    priority_gas_usd: float = Field(default=0.15)

    @field_validator("enabled_chains")
    @classmethod
    def _strip_chains(cls, v: str) -> str:
        return ",".join(part.strip().lower() for part in v.split(",") if part.strip())

    @property
    def chain_keys(self) -> list[str]:
        return [c for c in self.enabled_chains.split(",") if c]

    @property
    def admin_ids(self) -> set[int]:
        out: set[int] = set()
        for part in self.telegram_admin_ids.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return out

    def rpc_override(self, chain_key: str) -> list[str]:
        raw = getattr(self, f"{chain_key}_rpc_urls", "")
        return [u.strip() for u in raw.split(",") if u.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
