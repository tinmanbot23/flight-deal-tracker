"""Shared fixtures for the Travelpayouts-based tracker tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FILTERS_CONFIG = {
    "max_price_usd": 1000,
    "max_stops": 1,
    "blocked_carriers": ["NK", "F9", "G4", "XP", "SY", "OG", "N0", "MX", "Y4", "VB"],
}


def make_ticket(
    origin="RDU",
    destination="DEN",
    price=249,
    transfers=1,
    airline="DL",
    flight_number=1103,
    depart_date="2026-09-20",
    dep_hhmm="11:40",
    length=5,
) -> dict:
    """A Travelpayouts calendar ticket dict (as returned per departure date)."""
    from datetime import date, timedelta
    ret = (date.fromisoformat(depart_date) + timedelta(days=length)).isoformat()
    return {
        "origin": origin,
        "destination": destination,
        "price": price,
        "transfers": transfers,
        "airline": airline,
        "flight_number": flight_number,
        "departure_at": f"{depart_date}T{dep_hhmm}:00Z",
        "return_at": f"{ret}T16:40:00Z",
        "expires_at": f"{depart_date}T00:00:00Z",
    }


def make_offer_row(**overrides) -> dict:
    """A complete offers-table row dict with sensible defaults."""
    row = {
        "run_timestamp": "2026-07-09T08:00:00Z",
        "origin": "RDU",
        "dest": "DEN",
        "dest_region": "domestic",
        "depart_date": "2026-09-20",
        "return_date": "2026-09-25",
        "price_usd": 249.0,
        "currency": "USD",
        "airline": "DL",
        "flight_number": "1103",
        "stops": 1,
        "departure_at": "2026-09-20T11:40:00Z",
        "return_at": "2026-09-25T16:40:00Z",
        "expires_at": "2026-09-20T00:00:00Z",
        "offer_hash": "deadbeef00000000",
    }
    row.update(overrides)
    return row


@pytest.fixture()
def ticket() -> dict:
    """A clean ticket that passes every filter (from RDU, floor 10:00)."""
    return make_ticket()


@pytest.fixture()
def filters_config() -> dict:
    return dict(FILTERS_CONFIG)
