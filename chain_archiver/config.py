"""Settings and the symbol universe.

Phase 1 keeps the watchlist hardcoded here. Phase 2 moves it to
data/watchlist.yaml (version controlled) without changing SymbolSpec.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROD_API_URL = "https://api.tastyworks.com"
CERT_API_URL = "https://api.cert.tastyworks.com"

# Sent on production requests only; the sandbox rejects it.
API_VERSION = "20251101"

EASTERN = ZoneInfo("America/New_York")

# /market-data/by-type accepts at most 100 symbols across all instrument types.
QUOTE_CHUNK_SIZE = 100

# /market-metrics takes a comma-joined list; kept well under any URL limit.
METRICS_CHUNK_SIZE = 50


@dataclass(frozen=True)
class SymbolSpec:
    """One underlying and the filters applied to its chain.

    max_dte / monthly_max_dte / strike_pct implement the expiration and strike
    filters in §6. They are per-symbol so a symbol whose interesting structure
    lives further out can be widened without widening the whole universe.
    """

    symbol: str
    #: Index options (SPX, NDX, RUT, VIX) quote under a different instrument
    #: type and settle differently. This drives both the underlying quote
    #: request and the is_index_option column.
    is_index: bool = False
    #: Archive every expiration out to this many calendar days.
    max_dte: int = 120
    #: Beyond max_dte, keep monthly ("Regular") expirations out to here.
    monthly_max_dte: int = 365
    #: Keep strikes within this fraction of spot.
    strike_pct: float = 0.35


# Phase 1: five liquid names, enough to prove the pipeline and start the
# archive accumulating tonight. Expanded to ~30 in Phase 2 (§11).
PHASE1_WATCHLIST: tuple[SymbolSpec, ...] = (
    SymbolSpec("SPY"),
    SymbolSpec("QQQ"),
    SymbolSpec("IWM"),
    SymbolSpec("AAPL"),
    SymbolSpec("NVDA"),
)


@dataclass(frozen=True)
class Settings:
    client_secret: str
    refresh_token: str
    api_url: str
    data_dir: Path
    send_api_version: bool

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        secret = os.environ.get("TT_CLIENT_SECRET", "").strip()
        refresh = os.environ.get("TT_REFRESH_TOKEN", "").strip()
        missing = [
            name
            for name, value in (
                ("TT_CLIENT_SECRET", secret),
                ("TT_REFRESH_TOKEN", refresh),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Copy .env.example to .env and fill them in."
            )

        is_cert = os.environ.get("TT_ENV", "production").lower() == "cert"
        return cls(
            client_secret=secret,
            refresh_token=refresh,
            api_url=CERT_API_URL if is_cert else PROD_API_URL,
            data_dir=Path(os.environ.get("ARCHIVE_DATA_DIR", "data")).resolve(),
            send_api_version=not is_cert,
        )
