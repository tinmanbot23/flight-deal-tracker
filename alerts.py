"""Pushover notifications for top deals and price-threshold hits.

Uses PUSHOVER_TOKEN / PUSHOVER_USER env vars; when unset, alerts are logged
instead of sent so the pipeline still works end to end.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date
from typing import Any

import requests

import db
import rank

log = logging.getLogger("alerts")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Friendly names for the airlines that turn up most on these routes; unknown
# codes fall back to the raw IATA code.
AIRLINE_NAMES = {
    "DL": "Delta", "AA": "American", "UA": "United", "B6": "JetBlue",
    "AS": "Alaska", "WN": "Southwest", "AC": "Air Canada", "LH": "Lufthansa",
    "BA": "British Airways", "AF": "Air France", "KL": "KLM", "IB": "Iberia",
    "TP": "TAP", "EI": "Aer Lingus", "AV": "Avianca", "CM": "Copa",
    "LA": "LATAM", "AM": "Aeroméxico",
}


def airline_label(code: str | None) -> str:
    """'DL' -> 'Delta (DL)'; unknown/None -> the code or '?'."""
    if not code:
        return "?"
    name = AIRLINE_NAMES.get(code)
    return f"{name} ({code})" if name else code


def fmt_date_range(depart: str, return_: str) -> str:
    """'2026-11-09', '2026-11-17' -> 'Nov 9-17' (or 'Nov 28-Dec 5')."""
    start = date.fromisoformat(depart)
    if not return_:
        return f"{start.strftime('%b')} {start.day}"
    end = date.fromisoformat(return_)
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}-{end.day}"
    return f"{start.strftime('%b')} {start.day}-{end.strftime('%b')} {end.day}"


def stops_label(stops: int) -> str:
    """0 -> 'nonstop', 1 -> '1 stop', n -> 'n stops'."""
    return "nonstop" if stops == 0 else (f"{stops} stop" if stops == 1 else f"{stops} stops")


def format_deal(deal: dict[str, Any]) -> str:
    """Bookable one-line summary, e.g.
    'RDU->DEN $249 · Delta (DL) 1103 · nonstop · Nov 9-14'."""
    flight = f" {deal['flight_number']}" if deal.get("flight_number") else ""
    return (
        f"{deal['origin']}->{deal['dest']} ${deal['price_usd']:.0f} · "
        f"{airline_label(deal.get('airline'))}{flight} · "
        f"{stops_label(int(deal['stops']))} · "
        f"{fmt_date_range(deal['depart_date'], deal.get('return_date', ''))}"
    )


def send_pushover(title: str, message: str) -> bool:
    """Send via Pushover; log-only (returns False) when credentials are unset."""
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        log.info("Pushover unset; would send [%s]:\n%s", title, message)
        return False
    response = requests.post(
        PUSHOVER_URL,
        data={"token": token, "user": user, "title": title, "message": message},
        timeout=30,
    )
    if response.status_code != 200:
        log.error("Pushover send failed (%d): %s", response.status_code, response.text[:300])
        return False
    log.info("Pushover alert sent: %s", title)
    return True


def build_top_message(tops: dict[str, list[dict[str, Any]]]) -> str:
    """Message body listing the top 3 domestic and international deals."""
    lines: list[str] = []
    for region in ("domestic", "international"):
        lines.append(f"— {region.upper()} —")
        deals = tops.get(region, [])
        if not deals:
            lines.append("(no qualifying offers this run)")
        for deal in deals:
            lines.append(format_deal(deal))
    return "\n".join(lines)


def threshold_deals(conn: sqlite3.Connection, config: dict) -> list[dict[str, Any]]:
    """Cheapest current offer per route below max_price_usd that has not been
    alerted (by offer_hash) within the dedupe window."""
    run_timestamp = db.latest_run_timestamp(conn)
    if run_timestamp is None:
        return []
    max_price = config["filters"]["max_price_usd"]
    dedupe_hours = config["alerts"]["threshold_dedupe_hours"]

    cheapest_per_route: dict[tuple[str, str], sqlite3.Row] = {}
    for row in db.offers_for_run(conn, run_timestamp):
        if row["price_usd"] >= max_price:
            continue
        key = (row["origin"], row["dest"])
        current = cheapest_per_route.get(key)
        if current is None or row["price_usd"] < current["price_usd"]:
            cheapest_per_route[key] = row

    deals = []
    for row in cheapest_per_route.values():
        if not db.was_alerted_recently(conn, row["offer_hash"], dedupe_hours):
            deals.append(rank.row_to_deal(row))
    deals.sort(key=lambda d: d["price_usd"])
    return deals


def main() -> int:
    """Send the top-3 summary plus deduped threshold alerts for the latest run."""
    import tracker

    config = tracker.load_config()
    conn = db.connect()
    if db.latest_run_timestamp(conn) is None:
        log.info("No offers in database; nothing to alert.")
        return 0

    tops = rank.rank_all(conn, config)
    send_pushover("Flight deals · top picks", build_top_message(tops))
    for deal in (d for region in tops.values() for d in region):
        db.record_alert(conn, deal["offer_hash"])

    pending = threshold_deals(conn, config)
    cap = config["alerts"]["max_threshold_alerts_per_run"]
    if pending:
        shown = pending[:cap]
        body = "\n".join(format_deal(deal) for deal in shown)
        if len(pending) > cap:
            body += f"\n(+{len(pending) - cap} more below threshold)"
        send_pushover("Flight deals · below price threshold", body)
        for deal in shown:
            db.record_alert(conn, deal["offer_hash"])
    else:
        log.info("No new threshold deals to alert.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
