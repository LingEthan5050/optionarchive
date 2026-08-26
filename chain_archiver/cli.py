"""Command line entry point.

Phase 1 exposes one command: snapshot. The trading-day guard, run log,
verify and derive commands arrive in later phases; this deliberately runs
manually so the archive can start accumulating before any of that exists.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chain_archiver import calendar as trading_calendar
from chain_archiver import fetch, writer
from chain_archiver.auth import AuthError, TastytradeClient
from chain_archiver.config import EASTERN, WATCHLIST, Settings, SymbolSpec
from chain_archiver.schema import CHAINS_SCHEMA, METRICS_SCHEMA, build_table

log = logging.getLogger("chain_archiver")

#: A run that loses more than this share of its symbols is structurally
#: broken rather than noisy, and says so in its exit code (section 8).
FAILURE_ALERT_THRESHOLD = 0.20


@dataclass
class SymbolResult:
    symbol: str
    rows: int = 0
    contracts: int = 0
    quoted: int = 0
    underlying_price: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def snapshot_symbol(
    client: TastytradeClient,
    spec: SymbolSpec,
    *,
    snapshot_ts: datetime,
    session: str,
) -> tuple[list[dict], SymbolResult]:
    """Fetch one symbol. Raises nothing the caller should not isolate."""
    result = SymbolResult(symbol=spec.symbol)

    underlying_price = fetch.fetch_underlying_price(client, spec)
    if underlying_price is None:
        # Without spot there is no strike filter, and archiving the entire
        # unfiltered chain would quietly poison the partition. Fail the
        # symbol instead.
        raise RuntimeError("no underlying quote returned; cannot apply strike filter")
    result.underlying_price = underlying_price

    chain_items = fetch.fetch_nested_chain(client, spec.symbol)
    contracts = fetch.select_contracts(chain_items, spec, underlying_price)
    result.contracts = len(contracts)
    if not contracts:
        raise RuntimeError("chain returned no contracts inside the configured filters")

    quotes = fetch.fetch_option_quotes(client, [c["occ_symbol"] for c in contracts])
    result.quoted = len(quotes)

    rows = fetch.build_chain_rows(
        contracts,
        quotes,
        snapshot_ts=snapshot_ts,
        session=session,
        underlying_price=underlying_price,
    )
    result.rows = len(rows)
    return rows, result


def run_snapshot(
    session: str,
    specs: tuple[SymbolSpec, ...],
    settings: Settings,
    dry_run: bool,
    force: bool = False,
) -> int:
    snapshot_ts = datetime.now(timezone.utc)
    now_et = snapshot_ts.astimezone(EASTERN)
    trade_date = now_et.date()

    # A scheduler fires this job on a fixed clock; the calendar decides
    # whether this particular firing should do anything. Both skips are
    # successful outcomes, not failures, so they exit 0 - otherwise every
    # weekend would look like an outage to the monitoring (section 6).
    if not force:
        try:
            target = trading_calendar.check_runnable(session, now_et)
        except trading_calendar.NotATradingDay as exc:
            log.info("Skipping: %s", exc)
            return 0
        except trading_calendar.WrongTimeForSession as exc:
            log.info("Skipping: %s", exc)
            return 0
        if trading_calendar.is_early_close(trade_date):
            log.info("Early close today; %s target is %s ET", session, f"{target:%H:%M}")

    log.info(
        "Snapshot %s session=%s at %s (%d symbols)%s",
        trade_date,
        session,
        snapshot_ts.isoformat(timespec="seconds"),
        len(specs),
        " [dry run]" if dry_run else "",
    )

    client = TastytradeClient(settings)
    try:
        # Fail loudly and immediately on bad credentials rather than
        # discovering it symbol by symbol (section 6).
        client.authenticate()
    except AuthError as exc:
        log.error("Authentication failed: %s", exc)
        client.close()
        return 2

    chain_rows: list[dict] = []
    results: list[SymbolResult] = []

    try:
        for spec in specs:
            try:
                rows, result = snapshot_symbol(
                    client, spec, snapshot_ts=snapshot_ts, session=session
                )
                chain_rows.extend(rows)
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                result = SymbolResult(symbol=spec.symbol, error=str(exc))
                log.warning("Symbol %s failed: %s", spec.symbol, exc)
            results.append(result)

        metric_items = fetch.fetch_metrics(client, [s.symbol for s in specs])
    finally:
        client.close()

    metrics_rows = fetch.build_metrics_rows(
        metric_items, snapshot_ts=snapshot_ts, session=session
    )

    # Schema mismatch is a hard failure. Do not coerce (section 6).
    chains_table = build_table(chain_rows, CHAINS_SCHEMA)
    metrics_table = build_table(metrics_rows, METRICS_SCHEMA)

    _report(results, chains_table.num_rows, metrics_table.num_rows)

    failed = [r for r in results if not r.ok]
    if len(failed) == len(specs):
        log.error("Every symbol failed; nothing worth writing.")
        return 1

    if dry_run:
        log.info("Dry run: validated %d chain rows, wrote nothing.", len(chain_rows))
        return 0

    writer.write_partition(
        chains_table, settings.data_dir, "chains", trade_date, session
    )
    if metrics_table.num_rows:
        writer.write_partition(
            metrics_table, settings.data_dir, "metrics", trade_date, session
        )
    else:
        log.warning("No metrics rows returned; metrics partition not written.")

    if len(failed) / len(specs) > FAILURE_ALERT_THRESHOLD:
        log.error(
            "%d/%d symbols failed, above the %.0f%% threshold.",
            len(failed),
            len(specs),
            FAILURE_ALERT_THRESHOLD * 100,
        )
        return 1
    return 0


def _report(results: list[SymbolResult], chain_rows: int, metric_rows: int) -> None:
    log.info("%-8s %8s %8s %8s  %s", "symbol", "spot", "rows", "quoted", "status")
    for r in results:
        spot = f"{r.underlying_price:.2f}" if r.underlying_price else "-"
        status = "ok" if r.ok else f"FAILED: {r.error}"
        log.info("%-8s %8s %8d %8d  %s", r.symbol, spot, r.rows, r.quoted, status)
    log.info("Totals: %d chain rows, %d metric rows", chain_rows, metric_rows)


def _resolve_specs(raw: str | None) -> tuple[SymbolSpec, ...]:
    if not raw:
        return WATCHLIST
    wanted = [s.strip().upper() for s in raw.split(",") if s.strip()]
    known = {s.symbol: s for s in WATCHLIST}
    # An unknown symbol gets the default filters rather than an error, so
    # ad-hoc runs work without editing config.
    return tuple(known.get(sym, SymbolSpec(sym)) for sym in wanted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archiver", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="capture one snapshot now")
    snap.add_argument("--session", required=True, choices=["am", "pm"])
    snap.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and validate against the schema, write nothing",
    )
    snap.add_argument(
        "--symbols", help="comma-separated override of the watchlist, e.g. SPY,QQQ"
    )
    snap.add_argument("--data-dir", help="override ARCHIVE_DATA_DIR")
    snap.add_argument(
        "--force",
        action="store_true",
        help="bypass the trading-day and session-time guards (ad-hoc runs)",
    )
    snap.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        settings = Settings.from_env()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2

    if args.data_dir:
        settings = Settings(
            client_secret=settings.client_secret,
            refresh_token=settings.refresh_token,
            api_url=settings.api_url,
            data_dir=Path(args.data_dir).resolve(),
            send_api_version=settings.send_api_version,
        )

    return run_snapshot(
        session=args.session,
        specs=_resolve_specs(args.symbols),
        settings=settings,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
