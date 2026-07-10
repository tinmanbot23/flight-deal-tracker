"""Read-only analysis of recent tracker data. Answers three questions:

  1. How many offers did each filter reject (per filter)?
  2. Which routes never return a qualifying fare (drop candidates)?
  3. Is basic-economy detection catching branded fares worth whitelisting?

Requires the `rejections` instrumentation (see db.py / tracker.py): rejected
offers and their reasons are recorded there. Runs never call the API — this
only reads prices.db.

Usage:
    python analyze.py [--days 7] [--config config.yml] [--db prices.db]
"""
from __future__ import annotations

import argparse
import json
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


def basic_economy_candidates(
    rejection_rows: Iterable[Row], patterns: list[str]
) -> dict[str, Any]:
    """Distinct fare-brand names rejected as basic economy, ranked by how many
    offers carried them, with the trigger pattern(s) that matched. These are
    the review set for the fare_brand_whitelist. Offers rejected only for a
    missing/blank brand are counted separately (not whitelistable)."""
    brand_offers: Counter = Counter()
    brand_patterns: dict[str, list[str]] = {}
    missing_brand_offers = 0
    total_basic = 0

    for r in rejection_rows:
        if r["filter_reason"] != "basic_economy":
            continue
        total_basic += 1
        brands = json.loads(r["fare_brand_names"]) if r["fare_brand_names"] else []
        row_brands: set[str] = set()
        has_missing = False
        for b in brands:
            if b is None or not str(b).strip():
                has_missing = True
                continue
            matched = [p for p in patterns if p.upper() in str(b).upper()]
            if matched:
                key = str(b).strip()
                row_brands.add(key)
                brand_patterns[key] = matched
        for key in row_brands:
            brand_offers[key] += 1
        if has_missing and not row_brands:
            missing_brand_offers += 1

    candidates = [
        {"brand": brand, "offers": count, "matched_patterns": brand_patterns[brand]}
        for brand, count in brand_offers.most_common()
    ]
    return {
        "candidates": candidates,
        "missing_brand_offers": missing_brand_offers,
        "total_basic_rejections": total_basic,
    }


# --------------------------------------------------------------------------
# Text report
# --------------------------------------------------------------------------
def format_report(
    days: int,
    tally: Counter,
    health: list[dict[str, Any]],
    basic: dict[str, Any],
) -> str:
    lines: list[str] = [f"=== Tracker analysis · trailing {days} days ===", ""]

    # 1. Per-filter rejection tally.
    total = sum(tally.values())
    lines.append(f"1. FILTER REJECTIONS  ({total} offers rejected)")
    if total == 0:
        lines.append("   (no rejections recorded — has the tracker run since instrumentation landed?)")
    else:
        for reason, count in tally.most_common():
            lines.append(f"   {reason:18} {count:6d}  ({100 * count / total:4.1f}%)")
    lines.append("")

    # 2. Route health / drop candidates.
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
    lines.append("")

    # 3. Basic-economy whitelist review.
    lines.append(
        f"3. BASIC-ECONOMY REVIEW  ({basic['total_basic_rejections']} basic-economy "
        f"rejections; {basic['missing_brand_offers']} for missing/blank brand only)"
    )
    lines.append("   Distinct branded fares rejected — review for fare_brand_whitelist:")
    if basic["candidates"]:
        for c in basic["candidates"]:
            pats = ", ".join(c["matched_patterns"])
            lines.append(f"     {c['brand']:32} {c['offers']:5d} offers  (matched: {pats})")
    else:
        lines.append("     (none — no named brands were rejected as basic economy)")
    lines.append("")
    lines.append("   NOTE: whitelist a brand only if it genuinely includes seat selection.")
    return "\n".join(lines)


def run_analysis(conn, config: dict, days: int) -> str:
    """Read the DB and produce the text report."""
    offer_rows = db.offers_since(conn, days)
    rejection_rows = db.rejections_since(conn, days)
    tally = tally_rejections(rejection_rows)
    health = route_health(offer_rows, rejection_rows, load_configured_routes(config))
    basic = basic_economy_candidates(rejection_rows, config["filters"]["basic_economy_patterns"])
    return format_report(days, tally, health, basic)


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
