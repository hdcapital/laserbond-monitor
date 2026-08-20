"""lbl-tracker command line interface."""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lbl-tracker",
                                     description="External-data nowcast for ASX:LBL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="run ingesters (all by default)")
    p_ingest.add_argument("sources", nargs="*", help="subset of ingesters")
    p_ingest.add_argument("--backfill", action="store_true",
                          help="announcement ingest: pull deep history")
    p_ingest.add_argument("--strict", action="store_true",
                          help="exit non-zero if any source failed")

    p_probe = sub.add_parser("probe", help="live endpoint diagnostics (no writes)")
    p_probe.add_argument("sources", nargs="*")

    sub.add_parser("pulses", help="compute pulse composites -> parquet + docs json")
    sub.add_parser("dashboard", help="build static dashboard -> /docs")
    sub.add_parser("backtest", help="run backtest against manual LBL history")
    sub.add_parser("email", help="send the monthly email brief (SMTP secrets)")
    sub.add_parser("duckdb", help="rebuild the DuckDB views over parquet")
    sub.add_parser("status", help="print per-series freshness")

    args = parser.parse_args(argv)

    if args.command == "ingest":
        from .ingest import run_all
        if args.backfill:
            import lbl_tracker.ingest.asx_announcements as asx
            _orig = asx.ingest
            asx.ingest = lambda: _orig(backfill=True)  # noqa: E731
        results = run_all(args.sources or None)
        print(json.dumps(results, indent=1, default=str))
        failed = [r for r in results if r.get("status") == "error"]
        if failed:
            print(f"FAILED sources: {[r['source'] for r in failed]}", file=sys.stderr)
        return 1 if (failed and args.strict) else 0

    if args.command == "probe":
        from .probe import main as probe_main
        return probe_main(args.sources or None)

    if args.command == "pulses":
        from .analytics.pulses import compute_all
        result = compute_all(write=True)
        summary = {name: p.get("latest_value") for name, p in result["pulses"].items()}
        print(json.dumps(summary, indent=1))
        return 0

    if args.command == "dashboard":
        from .dashboard.build import build
        print(build())
        return 0

    if args.command == "backtest":
        from .analytics.backtest import run
        print(json.dumps(run(), indent=1, default=str))
        return 0

    if args.command == "email":
        from .email_brief import send
        send()
        return 0

    if args.command == "duckdb":
        from .store import build_duckdb
        print(build_duckdb())
        return 0

    if args.command == "status":
        import pandas as pd

        from .store import read_observations
        obs = read_observations()
        if obs.empty:
            print("store empty")
            return 0
        summary = (obs.dropna(subset=["value"])
                   .groupby("series_id")
                   .agg(rows=("value", "size"), first=("date", "min"),
                        last=("date", "max")))
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(summary.sort_index().to_string())
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
