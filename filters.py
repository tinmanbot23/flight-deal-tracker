"""Pure, unit-testable predicates over Travelpayouts ticket dicts.

A "ticket" is one value from the calendar endpoint's data map, augmented by
the tracker with its departure date. Its fields:
    origin, destination, price, transfers, airline, flight_number,
    departure_at, return_at, expires_at

The cached price data has no branded fares or per-segment routing, so the
main-cabin, connection-minimum, and per-direction duration filters that the
Amadeus version had are not possible here and have been removed. Each
surviving filter is a small pure function for isolated testing.
"""
from __future__ import annotations

import hashlib
from typing import Any

Ticket = dict[str, Any]


def price_usd(ticket: Ticket) -> float:
    """Ticket price."""
    return float(ticket["price"])


def price_ok(ticket: Ticket, max_price_usd: float) -> bool:
    """True if the ticket price is at or under the cap."""
    return price_usd(ticket) <= max_price_usd


def stops(ticket: Ticket) -> int:
    """Number of stops (transfers) on the outbound routing."""
    return int(ticket.get("transfers", 0) or 0)


def stops_ok(ticket: Ticket, max_stops: int) -> bool:
    """True if the ticket has at most max_stops connections."""
    return stops(ticket) <= max_stops


def departure_hhmm(ticket: Ticket) -> str:
    """Local departure time as 'HH:MM'.

    Travelpayouts labels departure_at with a 'Z' but the clock time is local
    to the origin airport, so the HH:MM substring is the local departure time
    — exactly what the per-origin floor is expressed against.
    """
    return str(ticket["departure_at"])[11:16]


def outbound_departure_ok(ticket: Ticket, earliest_hhmm: str) -> bool:
    """True if the outbound departs at or after the origin's earliest time."""
    return departure_hhmm(ticket) >= earliest_hhmm


def carrier(ticket: Ticket) -> str | None:
    """Marketing/validating airline IATA code (the only carrier the cached
    data exposes; there is no operating-carrier field)."""
    return ticket.get("airline")


def carriers_ok(ticket: Ticket, blocked: list[str]) -> bool:
    """True if the ticket's airline is not in the blocked list.

    Note: cached data exposes only the validating airline, so unlike the
    Amadeus version this cannot catch a blocked *operating* carrier.
    """
    code = carrier(ticket)
    return code is not None and code not in set(blocked)


def offer_hash(ticket: Ticket) -> str:
    """Stable dedupe key: same route + dates + flight => same hash."""
    parts = [
        str(ticket.get("origin", "")),
        str(ticket.get("destination", "")),
        str(ticket.get("departure_at", "")),
        str(ticket.get("return_at", "")),
        str(ticket.get("airline", "")),
        str(ticket.get("flight_number", "")),
    ]
    return hashlib.sha256("~".join(parts).encode()).hexdigest()[:16]


def evaluate_offer(
    ticket: Ticket, filters_config: dict[str, Any], earliest_departure: str
) -> str | None:
    """Run every supported filter; return None if the ticket passes, else the
    first failing filter's name (for the rejection record)."""
    if not carriers_ok(ticket, filters_config["blocked_carriers"]):
        return "blocked_carrier"
    if not price_ok(ticket, filters_config["max_price_usd"]):
        return "max_price"
    if not stops_ok(ticket, filters_config["max_stops"]):
        return "max_stops"
    if not outbound_departure_ok(ticket, earliest_departure):
        return "earliest_departure"
    return None
