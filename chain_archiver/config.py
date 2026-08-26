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


# The symbol universe. Around 30: wide enough to be useful, narrow enough
# that a snapshot stays fast and the archive stays focused.
#
# Breadth matters more than it looks, because it cannot be backfilled. A day
# captured at five symbols is a day the other twenty-five are gone for good,
# so this is deliberately at full width before the first scheduled run.
#
# Index options (SPX, NDX, RUT, VIX) are absent on purpose: they need
# is_index=True and that path has not been exercised against the live API
# yet. Add them once it has, not on the first unattended run.
WATCHLIST: tuple[SymbolSpec, ...] = (
    # Broad index ETFs - the backbone of the dataset.
    SymbolSpec("SPY"),
    SymbolSpec("QQQ"),
    SymbolSpec("IWM"),
    SymbolSpec("DIA"),
    # Sector, commodity and rates ETFs - vol regimes that do not move with
    # the index, which is the point of including them.
    SymbolSpec("XLE"),
    SymbolSpec("XLF"),
    SymbolSpec("XLK"),
    SymbolSpec("SMH"),
    SymbolSpec("GDX"),
    SymbolSpec("GLD"),
    SymbolSpec("TLT"),
    # High-IV single names with reliable, dateable earnings cycles - the
    # names an IV-rank screener would actually surface.
    SymbolSpec("AAPL"),
    SymbolSpec("MSFT"),
    SymbolSpec("NVDA"),
    SymbolSpec("AMD"),
    SymbolSpec("AVGO"),
    SymbolSpec("MU"),
    SymbolSpec("TSLA"),
    SymbolSpec("AMZN"),
    SymbolSpec("GOOGL"),
    SymbolSpec("META"),
    SymbolSpec("NFLX"),
    SymbolSpec("COIN"),
    SymbolSpec("PLTR"),
    SymbolSpec("BA"),
    SymbolSpec("DIS"),
    SymbolSpec("JPM"),
    SymbolSpec("XOM"),
    SymbolSpec("WMT"),
    SymbolSpec("CRM"),
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
