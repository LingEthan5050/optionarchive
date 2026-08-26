"""Settings and the symbol universe.

Phase 1 keeps the watchlist hardcoded here. Phase 2 moves it to
data/watchlist.yaml (version controlled) without changing SymbolSpec.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
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
    #:
    #: Calibrated per symbol to roughly 2.5 standard deviations at max_dte,
    #: because a single fixed band is not comparable across symbols: +/-35%
    #: is 5.1 sd on TLT but 0.7 sd on VIX, so the high-IV names an IV-rank
    #: screener actually surfaces were the most truncated.
    #:
    #: Two deliberate asymmetries. The floor stays at 0.35 even where a
    #: narrower band would suffice today, because these were calibrated in a
    #: low-vol regime (SPY IV 15%) and every band shrinks in sd terms when
    #: IV triples - narrowing now is exactly the vol-event regret. And
    #: strikes cannot be backfilled, so over-wide costs disk while too-narrow
    #: costs data permanently.
    strike_pct: float = 0.35


# The symbol universe. Around 30: wide enough to be useful, narrow enough
# that a snapshot stays fast and the archive stays focused.
#
# Breadth matters more than it looks, because it cannot be backfilled. A day
# captured at five symbols is a day the other twenty-five are gone for good,
# so this is deliberately at full width before the first scheduled run.
#
WATCHLIST: tuple[SymbolSpec, ...] = (
    # Broad index ETFs - the backbone of the dataset.
    SymbolSpec("SPY"),
    SymbolSpec("QQQ"),
    SymbolSpec("IWM"),
    SymbolSpec("DIA"),
    # Cash-settled index options. Each carries two roots: a standard
    # AM-settled monthly (SPX, NDX, RUT) and a PM-settled weekly (SPXW,
    # NDXP, RUTW). Both are archived, and settlement_type distinguishes
    # them - the distinction matters for expiration-day behaviour.
    SymbolSpec("SPX", is_index=True),
    SymbolSpec("NDX", is_index=True),
    SymbolSpec("RUT", is_index=True),
    # VIX is the one instrument where a symmetric band is simply the wrong
    # shape. It is mean-reverting with an enormous right tail - the exchange
    # lists strikes to 200 against a spot of 15 - and the far OTM calls are
    # the entire reason to archive it. +/-35% would stop at strike 20.
    SymbolSpec("VIX", is_index=True, strike_pct=4.00),
    # Sector, commodity and rates ETFs - vol regimes that do not move with
    # the index, which is the point of including them.
    SymbolSpec("XLE", strike_pct=0.40),
    SymbolSpec("XLF"),
    SymbolSpec("XLK", strike_pct=0.40),
    SymbolSpec("SMH", strike_pct=0.55),
    SymbolSpec("GDX", strike_pct=0.80),
    SymbolSpec("GLD", strike_pct=0.40),
    SymbolSpec("TLT"),
    # High-IV single names with reliable, dateable earnings cycles - the
    # names an IV-rank screener would actually surface.
    SymbolSpec("AAPL", strike_pct=0.40),
    SymbolSpec("MSFT", strike_pct=0.40),
    SymbolSpec("NVDA", strike_pct=0.65),
    SymbolSpec("AMD", strike_pct=0.80),
    SymbolSpec("AVGO", strike_pct=0.80),
    SymbolSpec("MU", strike_pct=1.00),
    SymbolSpec("TSLA", strike_pct=0.65),
    SymbolSpec("AMZN", strike_pct=0.50),
    SymbolSpec("GOOGL", strike_pct=0.50),
    SymbolSpec("META", strike_pct=0.55),
    SymbolSpec("NFLX", strike_pct=0.50),
    SymbolSpec("COIN", strike_pct=1.00),
    SymbolSpec("PLTR", strike_pct=0.80),
    SymbolSpec("BA", strike_pct=0.50),
    SymbolSpec("DIS"),
    SymbolSpec("JPM"),
    SymbolSpec("XOM", strike_pct=0.40),
    SymbolSpec("WMT"),
    SymbolSpec("CRM", strike_pct=0.80),
)


@dataclass(frozen=True)
class Settings:
    client_secret: str
    refresh_token: str
    api_url: str
    data_dir: Path
    send_api_version: bool
    #: Dead-man's switch endpoint. Unset means the job runs unmonitored
    #: rather than refusing to run.
    healthcheck_url: str | None = None

    def with_data_dir(self, data_dir: Path) -> "Settings":
        return replace(self, data_dir=data_dir.resolve())

    @classmethod
    def from_env(cls, require_credentials: bool = True) -> "Settings":
        load_dotenv()
        secret = os.environ.get("TT_CLIENT_SECRET", "").strip()
        refresh = os.environ.get("TT_REFRESH_TOKEN", "").strip()

        # derive, verify and status only read what is already on disk, so
        # they must work on a machine that has the archive but no keys.
        if require_credentials:
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
            healthcheck_url=os.environ.get("HEALTHCHECK_URL", "").strip() or None,
        )
