"""Main tracking run: rotate destinations, fetch calendar prices, filter, store.

For each selected origin x destination x date window, one API call per calendar
month spanning the window returns the cheapest round-trip ticket per departure
date. Tickets whose departure date falls inside the window and passes the
filters are stored; the rest are recorded as rejections.

Call-budget strategy: destinations rotate deterministically per run
(rotation.domestic_per_run + rotation.international_per_run), limited to
rotation.windows_per_run windows; calls per run = origins x destinations x
windows x (months spanning each window). A hard budget guard skips the run if
the month's recorded calls plus this run's planned calls exceed the cap.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from typing import Any

import yaml

import db
import filters
from travelpayouts_client import TravelpayoutsClient, TravelpayoutsError

log = logging.getLogger("tracker")


def load_config(path: str = "config.yml") -> dict[str, Any]:
    """Load and return the YAML configuration."""
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def months_in_window(start: date, end: date) -> list[str]:
    """The 'YYYY-MM' month keys spanning [start, end] inclusive."""
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def rotate(items: list, per_run: int, run_index: int) -> list:
    """Deterministic rotating slice so every item is covered over successive runs."""
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


def build_tasks(config: dict, run_index: int, today: date) -> list[dict]:
    """Every (origin, destination, window, month) calendar query for this run."""
    rotation = config["rotation"]
    tasks = []
    for origin in config["origins"]:
        for dest, region in select_destinations(config, run_index):
            for window in dest["date_windows"][: rotation["windows_per_run"]]:
                start = date.fromisoformat(str(window["depart_start"]))
                end = date.fromisoformat(str(window["depart_end"]))
                for month in months_in_window(start, end):
                    tasks.append({
                        "origin": origin["code"],
                        "earliest_departure": origin["earliest_departure"],
                        "dest": dest["code"],
                        "region": region,
                        "month": month,
                        "length": window["trip_length_days"],
                        "depart_start": start,
                        "depart_end": end,
                    })
    return tasks


def build_offer_row(
    ticket: dict,
    depart_date: str,
    *,
    run_timestamp: str,
    origin: str,
    dest: str,
    dest_region: str,
    currency: str,
) -> dict:
    """Flatten a passing ticket into an offers row."""
    return_at = ticket.get("return_at")
    return {
        "run_timestamp": run_timestamp,
        "origin": origin,
        "dest": dest,
        "dest_region": dest_region,
        "depart_date": depart_date,
        "return_date": (return_at or "")[:10],
        "price_usd": filters.price_usd(ticket),
        "currency": currency.upper(),
        "airline": ticket.get("airline"),
        "flight_number": str(ticket.get("flight_number", "")),
        "stops": filters.stops(ticket),
        "departure_at": ticket.get("departure_at"),
        "return_at": return_at,
        "expires_at": ticket.get("expires_at"),
        "offer_hash": filters.offer_hash(ticket),
    }


def build_rejection_row(
    ticket: dict,
    depart_date: str,
    *,
    run_timestamp: str,
    origin: str,
    dest: str,
    dest_region: str,
    reason: str,
) -> dict:
    """Flatten a rejected ticket into a rejections row. Defensive: recording a
    rejection must never crash the run."""
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
        "price_usd": safe(lambda: filters.price_usd(ticket), None),
        "filter_reason": reason,
        "airline": ticket.get("airline"),
        "stops": safe(lambda: filters.stops(ticket), None),
    }


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

        currency = config["search"]["currency"]
        client = TravelpayoutsClient(currency=currency, on_call=lambda: db.add_api_calls(conn, 1))
        filters_config = config["filters"]
        not_before = date.today() + timedelta(days=1)

        # Group tasks by route for one log line per route per run.
        routes: dict[tuple[str, str], list[dict]] = {}
        for task in tasks:
            routes.setdefault((task["origin"], task["dest"]), []).append(task)

        for (origin, dest), route_tasks in routes.items():
            fetched = passed = 0
            cheapest: float | None = None
            for task in route_tasks:
                try:
                    calendar = client.prices_calendar(
                        origin=origin,
                        destination=dest,
                        depart_month=task["month"],
                        length=task["length"],
                    )
                except TravelpayoutsError as exc:
                    log.warning("%s->%s %s: API error, skipping month: %s",
                                origin, dest, task["month"], exc)
                    continue
                for depart_date, ticket in calendar.items():
                    day = date.fromisoformat(depart_date)
                    if day < max(task["depart_start"], not_before) or day > task["depart_end"]:
                        continue
                    fetched += 1
                    try:
                        reason = filters.evaluate_offer(
                            ticket, filters_config, task["earliest_departure"]
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        log.warning("%s->%s: malformed ticket skipped: %s", origin, dest, exc)
                        continue
                    if reason is not None:
                        db.insert_rejection(conn, build_rejection_row(
                            ticket, depart_date,
                            run_timestamp=run_timestamp, origin=origin, dest=dest,
                            dest_region=task["region"], reason=reason,
                        ))
                        continue
                    try:
                        row = build_offer_row(
                            ticket, depart_date,
                            run_timestamp=run_timestamp, origin=origin, dest=dest,
                            dest_region=task["region"], currency=currency,
                        )
                    except (KeyError, ValueError, TypeError) as exc:
                        log.warning("%s->%s: malformed ticket skipped: %s", origin, dest, exc)
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
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH, help="Path to the SQLite database.")
    args = parser.parse_args(argv)
    return run(config_path=args.config, db_path=args.db)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
