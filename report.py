"""Regenerate report.md: top-3 deals per region plus per-route cheapest
current price with a 7-day trend."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db
import rank
import tracker
from alerts import format_deal

log = logging.getLogger("report")

OUTPUT_PATH = "report.md"


def route_trends(conn) -> list[dict]:
    """Per route: cheapest price in the last 24h vs cheapest 1-7 days ago."""
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    current: dict[tuple[str, str], float] = {}
    previous: dict[tuple[str, str], float] = {}
    for row in db.offers_since(conn, 7):
        key = (row["origin"], row["dest"])
        if row["run_timestamp"] >= day_ago:
            bucket = current
        elif row["run_timestamp"] >= week_ago:
            bucket = previous
        else:
            continue
        if key not in bucket or row["price_usd"] < bucket[key]:
            bucket[key] = row["price_usd"]

    trends = []
    for (origin, dest), price in sorted(current.items()):
        prior = previous.get((origin, dest))
        if prior is None:
            trend = "–"
        elif price < prior:
            trend = f"↓ ${prior - price:.0f}"
        elif price > prior:
            trend = f"↑ ${price - prior:.0f}"
        else:
            trend = "→ flat"
        trends.append({"origin": origin, "dest": dest, "price": price, "trend": trend})
    return trends


def build_report(conn, config: dict) -> str:
    """Assemble the full markdown report."""
    tops = rank.rank_all(conn, config)
    lines = [
        "# Flight deal report",
        "",
        f"_Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    for region in ("domestic", "international"):
        lines.append(f"## Top 3 {region}")
        lines.append("")
        deals = tops.get(region, [])
        if not deals:
            lines.append("_No qualifying offers in the latest run._")
        for i, deal in enumerate(deals, 1):
            lines.append(f"{i}. {format_deal(deal)}")
        lines.append("")

    lines.append("## Routes (cheapest current offer, 7-day trend)")
    lines.append("")
    trends = route_trends(conn)
    if trends:
        lines.append("| Route | Cheapest | 7d trend |")
        lines.append("|-------|---------:|----------|")
        for t in trends:
            lines.append(f"| {t['origin']}→{t['dest']} | ${t['price']:.0f} | {t['trend']} |")
    else:
        lines.append("_No offers recorded in the last 24 hours._")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    """Write report.md from the database."""
    config = tracker.load_config()
    conn = db.connect()
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        handle.write(build_report(conn, config))
    log.info("Wrote %s", OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
