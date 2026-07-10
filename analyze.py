"""Read-only analysis of recent tracker data. Answers:

  1. How many tickets did each filter reject (per filter)?
  2. Which routes never return a qualifying fare (drop candidates)?

Requires the `rejections` instrumentation (see db.py / tracker.py). Runs make
no API calls — this only reads prices.db.

Usage:
    python analyze.py [--days 7] [--config config.yml] [--db prices.db]
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from typing import Any, Iterable, Mapping

import db

log = logging.getLogger("analyze")

Row = Mapping[str, Any]


def load_configured_routes(config: dict) -> list[tuple[str, str, str]]:
    """Every (origin, dest, region) the config could query."""
    origins = [o["code"] for o in config["origins"]]
    routes: list[tuple[str, str, str]] = []
    for region, group in config["destinations"].items():
        for dest in group:
            for origin in origins:
                routes.append((origin, dest["code"], region))
    return routes


def tally_rejections(rejection_rows: Iterable[Row]) -> Counter:
    """Count rejections by filter_reason (first-failing-filter attribution)."""
    return Counter(r["filter_reason"] for r in rejection_rows)


def route_health(
    offer_rows: Iterable[Row],
    rejection_rows: Iterable[Row],
    configured_routes: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Per configured route: how many offers passed vs were rejected, and a
    status. 'drop_candidate' = returned fares but none ever qualified;
    'no_data' = nothing seen in the window (not sampled, or no inventory)."""
    passed: Counter = Counter()
    rejected: Counter = Counter()
    for o in offer_rows:
        passed[(o["origin"], o["dest"])] += 1
    for r in rejection_rows:
        rejected[(r["origin"], r["dest"])] += 1

    health = []
    for origin, dest, region in configured_routes:
        p = passed.get((origin, dest), 0)
        rj = rejected.get((origin, dest), 0)
        status = "healthy" if p > 0 else ("drop_candidate" if rj > 0 else "no_data")
        health.append({
            "origin": origin, "dest": dest, "region": region,
            "passed": p, "rejected": rj, "status": status,
        })
    return health


def format_report(days: int, tally: Counter, health: list[dict[str, Any]]) -> str:
    lines: list[str] = [f"=== Tracker analysis · trailing {days} days ===", ""]

    total = sum(tally.values())
    lines.append(f"1. FILTER REJECTIONS  ({total} tickets rejected)")
    if total == 0:
        lines.append("   (no rejections recorded — has the tracker run in this window?)")
    else:
        for reason, count in tally.most_common():
            lines.append(f"   {reason:18} {count:6d}  ({100 * count / total:4.1f}%)")
    lines.append("")

    drop = [h for h in health if h["status"] == "drop_candidate"]
    no_data = [h for h in health if h["status"] == "no_data"]
    healthy = [h for h in health if h["status"] == "healthy"]
    lines.append(
        f"2. ROUTE HEALTH  ({len(healthy)} healthy, {len(drop)} drop candidates, "
        f"{len(no_data)} no-data of {len(health)} routes)"
    )
    lines.append("   Drop candidates (returned fares, none ever qualified):")
    if drop:
        for h in sorted(drop, key=lambda x: -x["rejected"]):
            lines.append(f"     {h['origin']}->{h['dest']:4} ({h['region'][:4]})  "
                         f"rejected={h['rejected']}, passed=0")
    else:
        lines.append("     (none)")
    lines.append("   No data in window (not sampled, or no inventory returned):")
    if no_data:
        lines.append("     " + ", ".join(f"{h['origin']}->{h['dest']}" for h in no_data))
    else:
        lines.append("     (none)")
    return "\n".join(lines)


def run_analysis(conn, config: dict, days: int) -> str:
    """Read the DB and produce the text report."""
    offer_rows = db.offers_since(conn, days)
    rejection_rows = db.rejections_since(conn, days)
    tally = tally_rejections(rejection_rows)
    health = route_health(offer_rows, rejection_rows, load_configured_routes(config))
    return format_report(days, tally, health)


def main(argv: list[str] | None = None) -> int:
    import tracker  # local import to avoid a cycle at module load

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Trailing window (default 7).")
    parser.add_argument("--config", default="config.yml", help="Config YAML path.")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH, help="SQLite database path.")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    try:
        print(run_analysis(conn, tracker.load_config(args.config), args.days))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
