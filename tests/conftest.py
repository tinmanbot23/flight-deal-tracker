"""Shared fixtures: the canned Amadeus response and helpers for DB rows."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "amadeus_response.json"

FILTERS_CONFIG = {
    "max_price_usd": 1000,
    "max_stops": 1,
    "max_total_duration_hours": 14,
    "min_connection_minutes": 60,
    "cabin": "ECONOMY",
    "basic_economy_patterns": ["BASIC", "LIGHT", "SAVER", "ECO BASIC"],
    "blocked_carriers": ["NK", "F9", "G4", "XP", "SY", "OG", "N0", "MX", "Y4", "VB"],
}


@pytest.fixture()
def response() -> dict:
    """Full fixture API response (fresh copy per test)."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def good_offer(response) -> dict:
    """Offer 1: DL one-stop, Main Cabin — passes every filter."""
    return copy.deepcopy(response["data"][0])


@pytest.fixture()
def basic_offer(response) -> dict:
    """Offer 2: same flights sold as Basic Economy."""
    return copy.deepcopy(response["data"][1])


@pytest.fixture()
def filters_config() -> dict:
    return dict(FILTERS_CONFIG)


def make_row(**overrides) -> dict:
    """A complete offers-table row dict with sensible defaults."""
    segments = [{
        "carrier": "DL", "carrier_name": "DELTA AIR LINES",
        "flight_number": "DL1103", "operating_carrier": "DL",
        "depart_airport": "RDU", "depart_time": "2026-11-09T11:40:00",
        "arrive_airport": "ATL", "arrive_time": "2026-11-09T13:05:00",
        "aircraft": "AIRBUS A321", "duration_minutes": 85,
    }]
    row = {
        "run_timestamp": "2026-07-09T08:00:00Z",
        "origin": "RDU",
        "dest": "SCL",
        "dest_region": "international",
        "depart_date": "2026-11-09",
        "return_date": "2026-11-17",
        "price_usd": 842.0,
        "currency": "USD",
        "outbound_json": json.dumps(segments),
        "inbound_json": json.dumps(segments),
        "stops_outbound": 1,
        "stops_inbound": 1,
        "connection_airports": json.dumps(["ATL", "ATL"]),
        "total_duration_minutes": 1537,
        "fare_brand_names": json.dumps(["MAIN CABIN"] * 4),
        "offer_hash": "deadbeef00000000",
    }
    row.update(overrides)
    return row
