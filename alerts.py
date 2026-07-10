"""Pushover notifications for top deals and price-threshold hits.

Uses PUSHOVER_TOKEN / PUSHOVER_USER env vars; when unset, alerts are logged
instead of sent so the pipeline still works end to end.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime
from typing import Any

import requests

import db
import rank

log = logging.getLogger("alerts")

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def fmt_clock(iso_timestamp: str) -> str:
    """'2026-11-09T11:40:00' -> '11:40a'."""
    moment = datetime.fromisoformat(iso_timestamp)
    hour = moment.hour % 12 or 12
    suffix = "a" if moment.hour < 12 else "p"
    return f"{hour}:{moment.minute:02d}{suffix}"


def fmt_leg(segments: list[dict[str, Any]]) -> str:
    """One itinerary as 'DL1103 RDU 11:40a→ATL 1:05p, DL147 ATL 3:55p→SCL 5:10a+1'."""
    leg_start = date.fromisoformat(segments[0]["depart_time"][:10])
    parts = []
    for seg in segments:
        arrival_date = date.fromisoformat(seg["arrive_time"][:10])
        plus = (arrival_date - leg_start).days
        plus_marker = f"+{plus}" if plus > 0 else ""
        parts.append(
            f"{seg['flight_number']} {seg['depart_airport']} "
            f"{fmt_clock(seg['depart_time'])}→{seg['arrive_airport']} "
            f"{fmt_clock(seg['arrive_time'])}{plus_marker}"
        )
    return ", ".join(parts)


def fmt_date_range(depart: str, return_: str) -> str:
    """'2026-11-09', '2026-11-17' -> 'Nov 9–17' (or 'Nov 28–Dec 5')."""
    start = date.fromisoformat(depart)
    end = date.fromisoformat(return_)
    if start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}"
    return f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}"


def format_deal(deal: dict[str, Any]) -> str:
    """Bookable one-line summary of a deal, e.g.
    'RDU→SCL $842 · Delta Air Lines DL1103 RDU 11:40a→ATL 1:05p, ... · Main Cabin · Nov 9–17'.
    """
    carrier = deal["outbound"][0]["carrier_name"].title()
    brand = next((b for b in deal["fare_brands"] if b), "?")
    return (
        f"{deal['origin']}→{deal['dest']} ${deal['price_usd']:.0f} · "
        f"{carrier} {fmt_leg(deal['outbound'])} · "
        f"{str(brand).title()} · "
        f"{fmt_date_range(deal['depart_date'], deal['return_date'])}"
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
