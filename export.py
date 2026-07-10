"""Write docs/data/prices.json: last 30 days of offers grouped by route,
plus the current top-3 deals, for the GitHub Pages dashboard."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import db
import rank
import tracker

log = logging.getLogger("export")

OUTPUT_PATH = os.path.join("docs", "data", "prices.json")
EXPORT_DAYS = 30


def destination_names(config: dict) -> dict[str, str]:
    """IATA code -> friendly name for every configured destination."""
    return {
        dest["code"]: dest["name"]
        for group in config["destinations"].values()
        for dest in group
    }


def build_export(conn, config: dict) -> dict[str, Any]:
    """Assemble the dashboard payload."""
    names = destination_names(config)
    routes: dict[str, dict[str, Any]] = {}
    for row in db.offers_since(conn, EXPORT_DAYS):
        key = f"{row['origin']}-{row['dest']}"
        route = routes.setdefault(key, {
            "origin": row["origin"],
            "dest": row["dest"],
            "dest_name": names.get(row["dest"], row["dest"]),
            "region": row["dest_region"],
            "offers": [],
        })
        route["offers"].append(rank.row_to_deal(row))

    tops = rank.rank_all(conn, config)
    for deals in tops.values():
        for deal in deals:
            deal["dest_name"] = names.get(deal["dest"], deal["dest"])

    return {
        "generated_at": db.utc_now_iso(),
        "top": tops,
        "routes": routes,
    }


def main() -> int:
    """Regenerate docs/data/prices.json from the database."""
    config = tracker.load_config()
    conn = db.connect()
    payload = build_export(conn, config)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    total_offers = sum(len(route["offers"]) for route in payload["routes"].values())
    log.info("Wrote %s: %d routes, %d offers", OUTPUT_PATH, len(payload["routes"]), total_offers)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
