"""Main tracking run: rotate destinations, sample dates, fetch, filter, store.

Call-budget strategy: each run covers a deterministic rotating subset of
destinations (rotation.domestic_per_run + rotation.international_per_run),
one date window (rotation.windows_per_run) and rotation.dates_per_window
evenly spaced departure dates. With the defaults that is
3 origins x 4 destinations x 1 window x 2 dates = 24 calls/run,
~2,160/month at 3 runs/day — under the 2,500 monthly budget, which is also
enforced as a hard guard before every run.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from typing import Any

import yaml

import db
import filters
from amadeus_client import AmadeusClient, AmadeusError

log = logging.getLogger("tracker")


def load_config(path: str = "config.yml") -> dict[str, Any]:
    """Load and return the YAML configuration."""
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def sample_dates(start: date, end: date, count: int, not_before: date | None = None) -> list[date]:
    """`count` evenly spaced dates in [start, end], clamped to not_before.

    Returns fewer dates when the window is too narrow, empty when it is
    entirely in the past.
    """
    if not_before and start < not_before:
        start = not_before
    if start > end or count <= 0:
        return []
    span = (end - start).days
    if count == 1:
        return [start + timedelta(days=span // 2)]
    offsets = sorted({round(i * span / (count - 1)) for i in range(count)})
    return [start + timedelta(days=offset) for offset in offsets]


def rotate(items: list, per_run: int, run_index: int) -> list:
    """Deterministic rotating slice: successive run indices walk the list so
    every item is covered every ceil(len/per_run) runs."""
    if not items or per_run <= 0:
        return []
    if per_run >= len(items):
        return list(items)
    start = (run_index * per_run) % len(items)
    return [items[(start + i) % len(items)] for i in range(per_run)]


def select_destinations(config: dict, run_index: int) -> list[tuple[dict, str]]:
    """This run's destination subset as (destination, region) pairs."""
    rotation = config["rotation"]
    selected: list[tuple[dict, str]] = []
    for region, key in (("domestic", "domestic_per_run"), ("international", "international_per_run")):
        group = config["destinations"].get(region, [])
        for dest in rotate(group, rotation[key], run_index):
            selected.append((dest, region))
    return selected


def simplify_segments(segments: list[dict], dictionaries: dict) -> list[dict]:
    """Reduce raw Amadeus segments to the fields we store and display."""
    carrier_names = dictionaries.get("carriers", {})
    aircraft_names = dictionaries.get("aircraft", {})
    simplified = []
    for seg in segments:
        carrier = seg["carrierCode"]
        operating = (seg.get("operating") or {}).get("carrierCode") or carrier
        aircraft_code = (seg.get("aircraft") or {}).get("code", "")
        simplified.append({
            "carrier": carrier,
            "carrier_name": carrier_names.get(carrier, carrier),
            "flight_number": f"{carrier}{seg['number']}",
            "operating_carrier": operating,
            "depart_airport": seg["departure"]["iataCode"],
            "depart_time": seg["departure"]["at"],
            "arrive_airport": seg["arrival"]["iataCode"],
            "arrive_time": seg["arrival"]["at"],
            "aircraft": aircraft_names.get(aircraft_code, aircraft_code),
            "duration_minutes": filters.parse_duration_minutes(seg["duration"]),
        })
    return simplified


def build_offer_row(
    offer: dict,
    dictionaries: dict,
    *,
    run_timestamp: str,
    origin: str,
    dest: str,
    dest_region: str,
    depart_date: str,
    return_date: str,
) -> dict:
    """Flatten a raw (already filtered) offer into a database row."""
    return {
        "run_timestamp": run_timestamp,
        "origin": origin,
        "dest": dest,
        "dest_region": dest_region,
        "depart_date": depart_date,
        "return_date": return_date,
        "price_usd": filters.price_usd(offer),
        "currency": offer["price"].get("currency", "USD"),
        "outbound_json": json.dumps(
            simplify_segments(filters.itinerary_segments(offer, 0), dictionaries)
        ),
        "inbound_json": json.dumps(
            simplify_segments(filters.itinerary_segments(offer, 1), dictionaries)
        ),
        "stops_outbound": filters.itinerary_stops(offer, 0),
        "stops_inbound": filters.itinerary_stops(offer, 1),
        "connection_airports": json.dumps(filters.connection_airports(offer)),
        "total_duration_minutes": filters.total_duration_minutes(offer),
        "fare_brand_names": json.dumps(filters.fare_brand_names(offer)),
        "offer_hash": filters.offer_hash(offer),
    }


def build_rejection_row(
    offer: dict,
    *,
    run_timestamp: str,
    origin: str,
    dest: str,
    dest_region: str,
    depart_date: str,
    reason: str,
) -> dict:
    """Flatten a rejected offer into a rejections row.

    Deliberately defensive: a rejected offer may be malformed in the very way
    that got it rejected, so each field extraction is guarded — recording why
    an offer was dropped must never crash the run.
    """
    def safe(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - instrumentation must not raise
            return default

    return {
        "run_timestamp": run_timestamp,
        "origin": origin,
        "dest": dest,
        "dest_region": dest_region,
        "depart_date": depart_date,
        "price_usd": safe(lambda: filters.price_usd(offer), None),
        "filter_reason": reason,
        "fare_brand_names": json.dumps(safe(lambda: filters.fare_brand_names(offer), [])),
        "carriers": json.dumps(sorted(safe(lambda: filters.offer_carriers(offer), set()))),
    }


def build_tasks(config: dict, run_index: int, today: date) -> list[dict]:
    """Every (origin, destination, depart/return date) query for this run."""
    rotation = config["rotation"]
    not_before = today + timedelta(days=1)
    tasks = []
    for origin in config["origins"]:
        for dest, region in select_destinations(config, run_index):
            for window in dest["date_windows"][: rotation["windows_per_run"]]:
                start = date.fromisoformat(str(window["depart_start"]))
                end = date.fromisoformat(str(window["depart_end"]))
                dates = sample_dates(start, end, rotation["dates_per_window"], not_before)
                if not dates:
                    log.warning(
                        "Window %s..%s for %s is entirely in the past; skipping",
                        start, end, dest["code"],
                    )
                for depart in dates:
                    tasks.append({
                        "origin": origin["code"],
                        "earliest_departure": origin["earliest_departure"],
                        "dest": dest["code"],
                        "region": region,
                        "depart_date": depart.isoformat(),
                        "return_date": (
                            depart + timedelta(days=window["trip_length_days"])
                        ).isoformat(),
                    })
    return tasks


def run(config_path: str = "config.yml", db_path: str = db.DEFAULT_DB_PATH) -> int:
    """Execute one tracking run. Returns a process exit code."""
    config = load_config(config_path)
    conn = db.connect(db_path)
    try:
        run_index = db.record_run(conn)
        run_timestamp = db.utc_now_iso()

        tasks = build_tasks(config, run_index, date.today())
        budget = config["api_budget"]["monthly_max_calls"]
        used = db.get_month_calls(conn)
        if used + len(tasks) > budget:
            log.warning(
                "API budget exceeded: %d calls used this month + %d planned > %d max. "
                "Skipping run.", used, len(tasks), budget,
            )
            return 0
        log.info(
            "Run #%d: %d API calls planned (%d/%d used this month)",
            run_index, len(tasks), used, budget,
        )

        client = AmadeusClient(on_call=lambda: db.add_api_calls(conn, 1))
        search = config["search"]
        filters_config = config["filters"]

        # Group tasks by route so we emit exactly one log line per route per run.
        routes: dict[tuple[str, str], list[dict]] = {}
        for task in tasks:
            routes.setdefault((task["origin"], task["dest"]), []).append(task)

        for (origin, dest), route_tasks in routes.items():
            fetched = passed = 0
            cheapest: float | None = None
            for task in route_tasks:
                try:
                    response = client.search_flight_offers(
                        origin=origin,
                        destination=dest,
                        departure_date=task["depart_date"],
                        return_date=task["return_date"],
                        adults=search["adults"],
                        currency=search["currency"],
                        max_results=search["max_results"],
                        travel_class=filters_config["cabin"],
                    )
                except AmadeusError as exc:
                    log.warning("%s->%s %s: API error, skipping date: %s",
                                origin, dest, task["depart_date"], exc)
                    continue
                offers = response.get("data", [])
                dictionaries = response.get("dictionaries", {})
                fetched += len(offers)
                for offer in offers:
                    try:
                        reason = filters.evaluate_offer(
                            offer, filters_config, task["earliest_departure"]
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        log.warning("%s->%s: malformed offer skipped: %s", origin, dest, exc)
                        continue
                    if reason is not None:
                        db.insert_rejection(conn, build_rejection_row(
                            offer,
                            run_timestamp=run_timestamp,
                            origin=origin,
                            dest=dest,
                            dest_region=task["region"],
                            depart_date=task["depart_date"],
                            reason=reason,
                        ))
                        continue
                    try:
                        row = build_offer_row(
                            offer, dictionaries,
                            run_timestamp=run_timestamp,
                            origin=origin,
                            dest=dest,
                            dest_region=task["region"],
                            depart_date=task["depart_date"],
                            return_date=task["return_date"],
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        log.warning("%s->%s: malformed offer skipped: %s", origin, dest, exc)
                        continue
                    db.insert_offer(conn, row)
                    passed += 1
                    if cheapest is None or row["price_usd"] < cheapest:
                        cheapest = row["price_usd"]
            conn.commit()
            log.info(
                "%s->%s: fetched=%d passed=%d cheapest=%s",
                origin, dest, fetched, passed,
                f"${cheapest:.0f}" if cheapest is not None else "n/a",
            )

        log.info("Run complete: %d API calls made", client.calls_made)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. `--config`/`--db` allow a targeted dry run."""
    import argparse

    parser = argparse.ArgumentParser(description="Run one flight-tracking pass.")
    parser.add_argument("--config", default="config.yml", help="Path to config YAML.")
    parser.add_argument(
        "--db", default=db.DEFAULT_DB_PATH, help="Path to the SQLite database."
    )
    args = parser.parse_args(argv)
    return run(config_path=args.config, db_path=args.db)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
