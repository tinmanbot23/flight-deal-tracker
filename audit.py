"""Defense-in-depth: re-validate stored offers against every filter.

The filters run before an offer is written, but a bug in the storage path
(wrong field mapping, a mutated offer, a config drift) could let something
through. This module re-derives the filter checks purely from what landed in
the database, so any offer that slipped past is caught after the fact and can
be reported and removed. Pure functions, unit-testable without the API.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

import db
import filters

log = logging.getLogger("audit")


def _leg_segments(row: sqlite3.Row | dict, key: str) -> list[dict[str, Any]]:
    value = row[key]
    return json.loads(value) if isinstance(value, str) else value


def _elapsed_minutes(segments: list[dict[str, Any]]) -> int:
    """Wall-clock minutes from first departure to last arrival of a leg."""
    start = datetime.fromisoformat(segments[0]["depart_time"])
    end = datetime.fromisoformat(segments[-1]["arrive_time"])
    return int((end - start).total_seconds() // 60)


def _connection_gaps(segments: list[dict[str, Any]]) -> list[int]:
    gaps = []
    for prev, nxt in zip(segments, segments[1:]):
        arrive = datetime.fromisoformat(prev["arrive_time"])
        depart = datetime.fromisoformat(nxt["depart_time"])
        gaps.append(int((depart - arrive).total_seconds() // 60))
    return gaps


def audit_row(
    row: sqlite3.Row | dict, filters_config: dict, origins: dict[str, str]
) -> list[str]:
    """Return a list of filter violations for one stored offer (empty == clean).

    `origins` maps an origin code to its earliest-departure "HH:MM" floor.
    """
    violations: list[str] = []
    outbound = _leg_segments(row, "outbound_json")
    inbound = _leg_segments(row, "inbound_json")
    brands = _leg_segments(row, "fare_brand_names")

    if row["price_usd"] > filters_config["max_price_usd"]:
        violations.append(f"price ${row['price_usd']:.0f} > {filters_config['max_price_usd']}")

    max_stops = filters_config["max_stops"]
    for label, segs in (("outbound", outbound), ("inbound", inbound)):
        if len(segs) - 1 > max_stops:
            violations.append(f"{label} stops {len(segs) - 1} > {max_stops}")

    limit = filters_config["max_total_duration_hours"] * 60
    for label, segs in (("outbound", outbound), ("inbound", inbound)):
        if _elapsed_minutes(segs) > limit:
            violations.append(f"{label} duration {_elapsed_minutes(segs)}m > {limit}m")

    min_conn = filters_config["min_connection_minutes"]
    for label, segs in (("outbound", outbound), ("inbound", inbound)):
        for gap in _connection_gaps(segs):
            if gap < min_conn:
                violations.append(f"{label} connection {gap}m < {min_conn}m")

    blocked = set(filters_config["blocked_carriers"])
    for seg in outbound + inbound:
        hit = {seg.get("carrier"), seg.get("operating_carrier")} & blocked
        if hit:
            violations.append(f"blocked carrier {sorted(hit)} on {seg.get('flight_number')}")

    patterns = filters_config["basic_economy_patterns"]
    whitelist = filters_config.get("fare_brand_whitelist")
    for brand in brands:
        if filters.is_basic_economy(brand, patterns, whitelist):
            violations.append(f"basic/unidentifiable fare brand {brand!r}")
            break

    floor = origins.get(row["origin"])
    if floor is not None:
        dep_hhmm = outbound[0]["depart_time"][11:16]
        if dep_hhmm < floor:
            violations.append(f"outbound departs {dep_hhmm} < floor {floor}")

    return violations


def audit_run(
    conn: sqlite3.Connection, config: dict, run_timestamp: str | None = None
) -> dict[str, list[str]]:
    """Audit every offer from a run. Returns {offer_hash: [violations]} for
    any offer that violates a filter (empty dict == all clean)."""
    run_timestamp = run_timestamp or db.latest_run_timestamp(conn)
    if run_timestamp is None:
        return {}
    origins = {o["code"]: o["earliest_departure"] for o in config["origins"]}
    leaks: dict[str, list[str]] = {}
    for row in db.offers_for_run(conn, run_timestamp):
        problems = audit_row(row, config["filters"], origins)
        if problems:
            leaks[row["offer_hash"]] = problems
    return leaks
