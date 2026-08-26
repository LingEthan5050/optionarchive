"""Command line entry point.

    archiver snapshot --session pm            capture a snapshot now
    archiver snapshot --session pm --dry-run  fetch and validate, write nothing
    archiver derive --date 2026-08-26         compute greeks for one day
    archiver derive --all                     recompute everything
    archiver verify --date 2026-08-26         sanity-check a written partition
    archiver status                           recent runs, failures, coverage
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from chain_archiver import calendar as trading_calendar
from chain_archiver import derive as greeks_math
from chain_archiver import fetch, health, runlog, verify, writer
from chain_archiver.auth import AuthError, TastytradeClient
from chain_archiver.config import EASTERN, WATCHLIST, Settings, SymbolSpec
from chain_archiver.schema import (
    CHAINS_SCHEMA,
    GREEKS_SCHEMA,
    METRICS_SCHEMA,
    build_table,
)

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


# -- snapshot ------------------------------------------------------------


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
        # unfiltered chain would quietly poison the partition.
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
        except (
            trading_calendar.NotATradingDay,
            trading_calendar.WrongTimeForSession,
        ) as exc:
            log.info("Skipping: %s", exc)
            return 0
        if trading_calendar.is_early_close(trade_date):
            log.info("Early close today; %s target is %s ET", session, f"{target:%H:%M}")

    journal = runlog.RunLog(settings.data_dir)
    journal.start(trade_date, session, dry_run)
    health.ping(settings.healthcheck_url, suffix="/start")

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
        journal.finish(
            attempted=len(specs), ok=0, chain_rows=0, metric_rows=0,
            exit_code=2, failures=[], note=f"auth failed: {exc}",
        )
        health.ping(settings.healthcheck_url, suffix="/fail", body=str(exc))
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
    failures = [(r.symbol, r.error or "") for r in failed]

    def close_out(code: int, note: str | None = None) -> int:
        journal.finish(
            attempted=len(specs), ok=len(specs) - len(failed),
            chain_rows=chains_table.num_rows, metric_rows=metrics_table.num_rows,
            exit_code=code, failures=failures, note=note,
        )
        health.ping(
            settings.healthcheck_url,
            suffix="" if code == 0 else "/fail",
            body=note or f"{len(failed)}/{len(specs)} symbols failed",
        )
        return code

    if len(failed) == len(specs):
        log.error("Every symbol failed; nothing worth writing.")
        return close_out(1, "all symbols failed")

    if dry_run:
        log.info("Dry run: validated %d chain rows, wrote nothing.", len(chain_rows))
        return close_out(0, "dry run")

    writer.write_partition(
        chains_table, settings.data_dir, "chains", trade_date, session
    )
    if metrics_table.num_rows:
        writer.write_partition(
            metrics_table, settings.data_dir, "metrics", trade_date, session
        )
    else:
        log.warning("No metrics rows returned; metrics partition not written.")

    # Verify in the same job, while there is still context for what happened.
    checked = verify.verify_partition(
        settings.data_dir, trade_date, session, expected_symbols=len(specs)
    )
    for note in checked.notes:
        log.info("verify: %s", note)
    for problem in checked.problems:
        log.error("verify: %s", problem)
    if not checked.ok:
        return close_out(1, "; ".join(checked.problems))

    if len(failed) / len(specs) > FAILURE_ALERT_THRESHOLD:
        log.error(
            "%d/%d symbols failed, above the %.0f%% threshold.",
            len(failed), len(specs), FAILURE_ALERT_THRESHOLD * 100,
        )
        return close_out(1, f"{len(failed)}/{len(specs)} symbols failed")

    return close_out(0)


def _report(results: list[SymbolResult], chain_rows: int, metric_rows: int) -> None:
    log.info("%-8s %10s %8s %8s  %s", "symbol", "spot", "rows", "quoted", "status")
    for r in results:
        spot = f"{r.underlying_price:,.2f}" if r.underlying_price else "-"
        status = "ok" if r.ok else f"FAILED: {r.error}"
        log.info("%-8s %10s %8d %8d  %s", r.symbol, spot, r.rows, r.quoted, status)
    log.info("Totals: %d chain rows, %d metric rows", chain_rows, metric_rows)


# -- derive --------------------------------------------------------------


def _partitions(data_dir: Path, dataset: str) -> list[tuple[date, str, Path]]:
    found = []
    for path in sorted((data_dir / dataset).glob("date=*/session=*/data.parquet")):
        day = date.fromisoformat(path.parent.parent.name.removeprefix("date="))
        found.append((day, path.parent.name.removeprefix("session="), path))
    return found


def run_derive(settings: Settings, only: date | None, do_all: bool) -> int:
    partitions = _partitions(settings.data_dir, "chains")
    if only:
        partitions = [p for p in partitions if p[0] == only]
    if not partitions:
        log.error("No chains partitions to derive%s.", f" for {only}" if only else "")
        return 1
    if not do_all and not only:
        partitions = partitions[-1:]

    # tastytrade publishes the rate it uses, and the endpoint needs no auth,
    # so recomputing history never requires credentials.
    rate = greeks_math.DEFAULT_RISK_FREE_RATE
    try:
        client = TastytradeClient(settings)
        fetched = fetch.fetch_risk_free_rate(client)
        client.close()
        if fetched is not None:
            rate = fetched
            log.info("Risk-free rate from tastytrade: %.4f", rate)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch risk-free rate (%s); using %.4f", exc, rate)

    for day, session, path in partitions:
        chain_rows = pq.read_table(path).to_pylist()

        # Dividend yield comes from the archived rate over spot, which is
        # why dividend_rate is carried in metrics.
        yields: dict[str, float] = {}
        metrics_path = writer.partition_dir(
            settings.data_dir, "metrics", day, session
        ) / "data.parquet"
        if metrics_path.exists():
            spots = {r["underlying_symbol"]: r["underlying_price"] for r in chain_rows}
            for m in pq.read_table(metrics_path).to_pylist():
                spot = spots.get(m["symbol"])
                if m.get("dividend_rate") and spot:
                    yields[m["symbol"]] = m["dividend_rate"] / spot

        rows = greeks_math.derive_rows(
            chain_rows, risk_free_rate=rate, dividend_yields=yields
        )
        table = build_table(rows, GREEKS_SCHEMA)
        writer.write_partition(
            table, settings.data_dir / "derived", "greeks", day, session
        )
        log.info("%s %s: %s", day, session, greeks_math.summarize(rows))

    return 0


# -- verify / status -----------------------------------------------------


def run_verify(settings: Settings, only: date | None) -> int:
    partitions = _partitions(settings.data_dir, "chains")
    if only:
        partitions = [p for p in partitions if p[0] == only]
    if not partitions:
        log.error("No partitions to verify.")
        return 1

    bad = 0
    for day, session, _ in partitions:
        checked = verify.verify_partition(
            settings.data_dir, day, session, expected_symbols=len(WATCHLIST)
        )
        log.info(
            "%s %s %s  %8d rows  %2d symbols  %s",
            "OK  " if checked.ok else "FAIL", day, session, checked.rows,
            checked.symbols, "; ".join(checked.notes),
        )
        for problem in checked.problems:
            log.error("     %s", problem)
        bad += not checked.ok
    return 1 if bad else 0


def run_status(settings: Settings, limit: int) -> int:
    runs = runlog.recent_runs(settings.data_dir, limit)
    if not runs:
        log.info("No runs recorded yet at %s", settings.data_dir / "runs.db")
    else:
        log.info("%-10s %-4s %-7s %8s %9s %5s %4s",
                 "date", "sess", "ok", "rows", "wall", "exit", "dry")
        for r in runs:
            log.info(
                "%-10s %-4s %2d/%-4d %8d %8.1fs %5s %4s",
                r["trade_date"], r["session"], r["symbols_ok"],
                r["symbols_attempted"], r["chain_rows"], r["wall_seconds"] or 0.0,
                r["exit_code"], "yes" if r["dry_run"] else "",
            )

    failures = runlog.recent_failures(settings.data_dir, 10)
    if failures:
        log.info("")
        log.info("Recent symbol failures:")
        for f in failures:
            log.info("  %s %s %-6s %s", f["trade_date"], f["session"],
                     f["symbol"], f["error"][:90])

    chains = _partitions(settings.data_dir, "chains")
    derived = _partitions(settings.data_dir / "derived", "greeks")
    log.info("")
    log.info(
        "Archive: %d chain partitions, %d derived, spanning %s",
        len(chains), len(derived),
        f"{chains[0][0]} to {chains[-1][0]}" if chains else "nothing yet",
    )
    return 0


# -- entry point ---------------------------------------------------------


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
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--data-dir", help="override ARCHIVE_DATA_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="capture one snapshot now")
    snap.add_argument("--session", required=True, choices=["am", "pm"])
    snap.add_argument("--dry-run", action="store_true",
                      help="fetch and validate against the schema, write nothing")
    snap.add_argument("--symbols", help="comma-separated watchlist override")
    snap.add_argument("--force", action="store_true",
                      help="bypass the trading-day and session-time guards")

    der = sub.add_parser("derive", help="compute greeks from chains")
    der.add_argument("--date", help="YYYY-MM-DD; default is the latest partition")
    der.add_argument("--all", action="store_true", help="recompute every partition")

    ver = sub.add_parser("verify", help="sanity-check written partitions")
    ver.add_argument("--date", help="YYYY-MM-DD; default is every partition")

    stat = sub.add_parser("status", help="recent runs, failures and coverage")
    stat.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        # Only snapshot needs credentials; derive, verify and status read
        # what is already on disk.
        settings = Settings.from_env(require_credentials=args.command == "snapshot")
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2

    if args.data_dir:
        settings = settings.with_data_dir(Path(args.data_dir))

    only = date.fromisoformat(args.date) if getattr(args, "date", None) else None

    if args.command == "snapshot":
        return run_snapshot(
            session=args.session,
            specs=_resolve_specs(args.symbols),
            settings=settings,
            dry_run=args.dry_run,
            force=args.force,
        )
    if args.command == "derive":
        return run_derive(settings, only, args.all)
    if args.command == "verify":
        return run_verify(settings, only)
    if args.command == "status":
        return run_status(settings, args.limit)
    return 2


if __name__ == "__main__":
    sys.exit(main())
