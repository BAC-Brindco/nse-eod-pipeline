"""Configuration, loaded from environment / .env. No secrets in code."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = three levels up from this file (src/nse_eod/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings.

    Everything is overridable by environment variable, so moving from this
    laptop to the server is a matter of changing ``DATABASE_URL`` and nothing else.
    """

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- database
    database_url: str = Field(
        default="postgresql://nse:nsepipe_local_dev@127.0.0.1:5433/nse_eod",
        description="Direct (non-pooled) Postgres connection. Must permit DDL for `migrate`.",
    )
    db_pool_min: int = 1
    db_pool_max: int = 4
    db_statement_timeout_s: int = 600

    # ------------------------------------------------------------------ paths
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    log_dir: Path = Field(default=PROJECT_ROOT / "logs")
    heartbeat_path: Path = Field(
        default=PROJECT_ROOT / "data" / "heartbeat.json",
        description=(
            "Freshness stamp published by `last-success` for OUT-OF-PROCESS consumers "
            "(the morning brief). Its contents are DB-derived, never wall-clock, so a "
            "run that never happened still reads as stale even though the file itself "
            "was just rewritten. Consumers must read last_success_at, not the mtime."
        ),
    )

    # ------------------------------------------------------------- alerting
    alert_webhook_url: str | None = Field(
        default=None,
        description="Slack-compatible incoming webhook. Unset => findings persist to DB only.",
    )
    alert_dry_run: bool = False

    # ------------------------------------------------------------------ HTTP
    http_timeout_s: float = 45.0
    http_max_retries: int = 5
    http_backoff_base_s: float = 1.5
    http_rate_limit_per_s: float = 2.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # -------------------------------------------------------------- pipeline
    backfill_start: dt.date = dt.date(2020, 1, 1)
    ca_lookback_days: int = 90
    ca_lookahead_days: int = 90
    ca_window_days: int = 60  # NSE caps the CA API response; 60d proved reliable

    # The corporate-actions API is segmented by `index`, and `equities` does NOT
    # include the SME platform. Fetching only `equities` left SME splits and
    # bonuses entirely absent, which showed up as unexplained -80% to -96%
    # single-day drops in the adjusted panel for ISHAN, GOLDSTAR, CELLECOR,
    # COOLCAPS, VMARCIND, JSLL, MOS and SECL -- all SM/ST series.
    # Must stay in step with `universe_series`.
    ca_indices: list[str] = ["equities", "sme"]

    # validation thresholds
    prev_close_tolerance_pct: float = 0.5
    # Aligned with tools/audit_panel.py (>0.25 and c_adj > 5.0). A 20% bar with
    # no price floor fires CRITICAL on ordinary Indian small-cap days, which
    # would make the nightly task retry every night and destroy the signal.
    overnight_jump_pct: float = 25.0
    overnight_jump_min_price: float = 5.0
    rowcount_sigma: float = 4.0
    min_rowcount_floor: int = 1000
    adv_window: int = 20
    universe_min_adv: float = 0.0  # rupees; 0 disables the liquidity floor

    # Series admitted to `in_universe`. EQ is the main board; SM/ST are the SME
    # platform (SM = SME normal, ST = SME trade-for-trade).
    # Deliberately EXCLUDED by default:
    #   BE / BZ  -- trade-for-trade surveillance settlement on the main board.
    #               A name moves here temporarily when NSE restricts it, so it is
    #               not freely tradeable, but its bars ARE still stored in gold so
    #               a symbol's price series has no hole while it is suspended.
    # Set NSE_EOD_UNIVERSE_SERIES='["EQ"]' to revert to main-board-only.
    universe_series: list[str] = ["EQ", "SM", "ST"]

    # Below this date UDiFF does not exist; above it legacy is gone.
    # Both were measured against live NSE on 2026-08-31.
    udiff_first_date: dt.date = dt.date(2024, 1, 1)
    legacy_last_date: dt.date = dt.date(2024, 9, 30)
    delivery_first_date: dt.date = dt.date(2020, 1, 1)

    keep_raw_files: bool = True

    @field_validator("data_dir", "log_dir")
    @classmethod
    def _mkdir(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v

    @property
    def raw_dir(self) -> Path:
        d = self.data_dir / "raw"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def cache_dir(self) -> Path:
        d = self.data_dir / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
