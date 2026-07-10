"""Rank the latest run's offers into Top-3 domestic and international deals.

Value score: price divided by the route's trailing 30-day median. When a
route has thin history (< ranking.min_history_offers prior offers), fall
back to absolute price normalized against filters.max_price_usd so the two
scales stay comparable. Ties break on shorter total duration, then fewer
stops. The top 3 never contains two deals for the same destination.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from typing import Any

import db

log = logging.getLogger("rank")

REGIONS = ("domestic", "international")


def value_score(
    price: float, history: list[float], max_price: float, min_history: int
) -> float:
    """Lower is better. Ratio vs trailing median when history is deep enough,
    else price normalized by the configured price cap."""
    if len(history) >= min_history:
        return price / statistics.median(history)
    return price / max_price


def row_to_deal(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a stored offer row into a plain dict with parsed JSON fields."""
    deal = dict(row)
    deal["outbound"] = json.loads(deal.pop("outbound_json"))
    deal["inbound"] = json.loads(deal.pop("inbound_json"))
    deal["connection_airports"] = json.loads(deal["connection_airports"])
    deal["fare_brands"] = json.loads(deal.pop("fare_brand_names"))
    return deal


def top_deals(
    conn: sqlite3.Connection, config: dict, region: str, top_n: int = 3
) -> list[dict[str, Any]]:
    """Top `top_n` deals for a region from the latest run, one per destination."""
    run_timestamp = db.latest_run_timestamp(conn)
    if run_timestamp is None:
        return []
    ranking = config["ranking"]
    max_price = config["filters"]["max_price_usd"]

    scored: list[dict[str, Any]] = []
    for row in db.offers_for_run(conn, run_timestamp, region):
        history = db.route_history_prices(
            conn, row["origin"], row["dest"], run_timestamp, ranking["history_days"]
        )
        deal = row_to_deal(row)
        deal["value_score"] = value_score(
            row["price_usd"], history, max_price, ranking["min_history_offers"]
        )
        scored.append(deal)

    scored.sort(
        key=lambda d: (
            d["value_score"],
            d["total_duration_minutes"],
            d["stops_outbound"] + d["stops_inbound"],
        )
    )

    top: list[dict[str, Any]] = []
    seen_destinations: set[str] = set()
    for deal in scored:
        if deal["dest"] in seen_destinations:
            continue
        top.append(deal)
        seen_destinations.add(deal["dest"])
        if len(top) == top_n:
            break
    return top


def rank_all(conn: sqlite3.Connection, config: dict) -> dict[str, list[dict[str, Any]]]:
    """Top 3 deals for both regions: {"domestic": [...], "international": [...]}."""
    return {region: top_deals(conn, config, region) for region in REGIONS}


if __name__ == "__main__":
    import tracker

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    connection = db.connect()
    tops = rank_all(connection, tracker.load_config())
    for region_name, deals in tops.items():
        log.info("Top %s deals:", region_name)
        for i, deal in enumerate(deals, 1):
            log.info(
                "  %d. %s->%s $%.0f (score %.2f) %s -> %s",
                i, deal["origin"], deal["dest"], deal["price_usd"],
                deal["value_score"], deal["depart_date"], deal["return_date"],
            )
