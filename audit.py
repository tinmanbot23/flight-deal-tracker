"""Defense-in-depth: re-validate stored offers against every filter.

The filters run before an offer is written, but a bug in the storage path
could let something through. This module re-derives the (supported) filter
checks purely from what landed in the database, so any offer that slipped
past is caught after the fact. Pure functions, unit-testable without the API.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

import db

log = logging.getLogger("audit")


def audit_row(
    row: sqlite3.Row | dict, filters_config: dict, origins: dict[str, str]
) -> list[str]:
    """Return a list of filter violations for one stored offer (empty == clean).

    `origins` maps an origin code to its earliest-departure "HH:MM" floor.
    """
    violations: list[str] = []

    if row["price_usd"] > filters_config["max_price_usd"]:
        violations.append(f"price ${row['price_usd']:.0f} > {filters_config['max_price_usd']}")

    if int(row["stops"]) > filters_config["max_stops"]:
        violations.append(f"stops {row['stops']} > {filters_config['max_stops']}")

    if row["airline"] in set(filters_config["blocked_carriers"]):
        violations.append(f"blocked carrier {row['airline']}")

    floor = origins.get(row["origin"])
    if floor is not None and row["departure_at"]:
        dep_hhmm = str(row["departure_at"])[11:16]
        if dep_hhmm < floor:
            violations.append(f"departs {dep_hhmm} < floor {floor}")

    return violations


def audit_run(
    conn: sqlite3.Connection, config: dict, run_timestamp: str | None = None
) -> dict[str, str]:
    """Audit every offer from a run. Returns {offer_hash: 'v1; v2'} for any
    offer that violates a filter (empty dict == all clean)."""
    run_timestamp = run_timestamp or db.latest_run_timestamp(conn)
    if run_timestamp is None:
        return {}
    origins = {o["code"]: o["earliest_departure"] for o in config["origins"]}
    leaks: dict[str, str] = {}
    for row in db.offers_for_run(conn, run_timestamp):
        problems = audit_row(row, config["filters"], origins)
        if problems:
            leaks[row["offer_hash"]] = "; ".join(problems)
    return leaks
