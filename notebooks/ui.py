"""Open DuckDB's browser UI over the archive.

    python notebooks/ui.py                # the real archive in data/
    python notebooks/ui.py /path/to/data  # any other archive root

This is the reading layer, kept as a separate program from the archiver so
either side can be rewritten without touching the other. It registers the
partitions as views named `chains`, `metrics` and `greeks` so the UI presents
tables rather than parquet globs.

Read-only by construction: the connection is in-memory and the views point at
the parquet files, so nothing typed into the UI can modify the archive.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
#: View name -> directory under the archive root. greeks lives under derived/
#: because it is regenerable from chains; the view name drops that prefix.
DATASETS = {
    "chains": Path("chains"),
    "metrics": Path("metrics"),
    "greeks": Path("derived", "greeks"),
}


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO / "data"
    if not root.exists():
        print(f"No archive at {root}\n")
        print("Nothing has been captured yet. Take a snapshot first:")
        print("  python -m chain_archiver.cli snapshot --session pm --force")
        return 1

    # In-memory database: the archive is only ever read through these views.
    con = duckdb.connect()

    registered = []
    for name, subdir in DATASETS.items():
        directory = root / subdir
        if not directory.exists() or not any(directory.rglob("*.parquet")):
            print(f"  {name:8} no partitions yet")
            continue
        glob = (directory / "**" / "*.parquet").as_posix()
        con.sql(
            f"CREATE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
        )
        rows = con.sql(f"SELECT count(*) FROM {name}").fetchone()[0]
        days = con.sql(f"SELECT count(DISTINCT date) FROM {name}").fetchone()[0]
        print(f"  {name:8} {rows:>9,} rows across {days} day(s)")
        registered.append(name)

    if not registered:
        print(f"\nArchive at {root} is empty - run a snapshot first.")
        return 1

    con.sql("INSTALL ui")
    con.sql("LOAD ui")
    con.sql("CALL start_ui()")

    url = con.sql("SELECT get_ui_url()").fetchone()[0]
    print(f"\nDuckDB UI: {url}")
    print("Views ready: " + ", ".join(registered))
    print("\nTry:  SELECT * FROM metrics ORDER BY implied_volatility_index_rank DESC;")
    print("Ctrl-C to stop.")

    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        con.sql("CALL stop_ui_server()")
        print("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
