"""Pure, unit-testable predicates and helpers over raw Amadeus flight-offer dicts.

Every filter is a small pure function taking the raw offer JSON (one element
of the API response's "data" list) plus its threshold, so each can be tested
in isolation against fixture data.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

Offer = dict[str, Any]

_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def parse_duration_minutes(value: str) -> int:
    """Parse an ISO-8601 duration like 'PT13H30M' or 'P1DT2H' into minutes."""
    match = _DURATION_RE.match(value or "")
    if not match or not any(match.groups()):
        raise ValueError(f"Unparseable ISO-8601 duration: {value!r}")
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return days * 24 * 60 + hours * 60 + minutes


def itinerary_segments(offer: Offer, index: int) -> list[dict[str, Any]]:
    """Segments of one itinerary: index 0 = outbound, 1 = inbound."""
    return offer["itineraries"][index]["segments"]


def all_segments(offer: Offer) -> list[dict[str, Any]]:
    """All segments across every itinerary, in order."""
    return [seg for itin in offer["itineraries"] for seg in itin["segments"]]


def price_usd(offer: Offer) -> float:
    """Grand total price of the offer."""
    price = offer["price"]
    return float(price.get("grandTotal") or price["total"])


def price_ok(offer: Offer, max_price_usd: float) -> bool:
    """True if the offer's grand total is at or under the price cap."""
    return price_usd(offer) <= max_price_usd


def itinerary_stops(offer: Offer, index: int) -> int:
    """Number of stops (connections) in one itinerary."""
    return max(len(itinerary_segments(offer, index)) - 1, 0)


def stops_ok(offer: Offer, max_stops: int) -> bool:
    """True if every itinerary has at most max_stops connections."""
    return all(
        itinerary_stops(offer, i) <= max_stops for i in range(len(offer["itineraries"]))
    )


def durations_ok(offer: Offer, max_hours: float) -> bool:
    """True if EACH direction's itinerary duration is at most max_hours."""
    limit = max_hours * 60
    return all(
        parse_duration_minutes(itin["duration"]) <= limit for itin in offer["itineraries"]
    )


def total_duration_minutes(offer: Offer) -> int:
    """Sum of all itinerary durations (outbound + inbound), in minutes."""
    return sum(parse_duration_minutes(itin["duration"]) for itin in offer["itineraries"])


def connection_gaps_minutes(offer: Offer) -> list[int]:
    """Layover lengths in minutes at every connection across all itineraries.

    Arrival and next departure are local times at the same airport, so their
    naive difference is the true connection time.
    """
    gaps: list[int] = []
    for itin in offer["itineraries"]:
        segs = itin["segments"]
        for prev, nxt in zip(segs, segs[1:]):
            arrive = datetime.fromisoformat(prev["arrival"]["at"])
            depart = datetime.fromisoformat(nxt["departure"]["at"])
            gaps.append(int((depart - arrive).total_seconds() // 60))
    return gaps


def connections_ok(offer: Offer, min_minutes: int) -> bool:
    """True if every connection is at least min_minutes long."""
    return all(gap >= min_minutes for gap in connection_gaps_minutes(offer))


def connection_airports(offer: Offer) -> list[str]:
    """IATA codes of every connection airport across all itineraries."""
    airports: list[str] = []
    for itin in offer["itineraries"]:
        for seg in itin["segments"][:-1]:
            airports.append(seg["arrival"]["iataCode"])
    return airports


def fare_brand_names(offer: Offer) -> list[str | None]:
    """Branded fare name per segment, in segment order; None where missing.

    Prefers the human label (brandedFareLabel) and falls back to the brand
    code (brandedFare). Padded with None if the airline returned fewer fare
    details than there are segments.
    """
    traveler_pricings = offer.get("travelerPricings") or [{}]
    names: list[str | None] = []
    for detail in traveler_pricings[0].get("fareDetailsBySegment") or []:
        names.append(detail.get("brandedFareLabel") or detail.get("brandedFare"))
    while len(names) < len(all_segments(offer)):
        names.append(None)
    return names


def is_basic_economy(
    brand: str | None,
    patterns: list[str],
    whitelist: list[str] | None = None,
) -> bool:
    """True if a fare brand is basic economy OR missing/unidentifiable.

    An exact (case-insensitive, trimmed) match against `whitelist` exempts a
    brand from the pattern check — the escape hatch for a legitimate branded
    fare whose name happens to contain a trigger word (e.g. an airline's
    "Economy Light Plus"). A missing/blank brand is rejected regardless of the
    whitelist: an unidentifiable fare can't be trusted to include seat
    selection. When in doubt, reject.
    """
    if not brand or not str(brand).strip():
        return True
    normalized = str(brand).strip()
    if whitelist and normalized.upper() in {w.strip().upper() for w in whitelist}:
        return False
    upper = normalized.upper()
    return any(pattern.upper() in upper for pattern in patterns)


def fare_brands_ok(
    offer: Offer, patterns: list[str], whitelist: list[str] | None = None
) -> bool:
    """True only if every segment has an identifiable, non-basic fare brand."""
    return not any(
        is_basic_economy(brand, patterns, whitelist) for brand in fare_brand_names(offer)
    )


def offer_carriers(offer: Offer) -> set[str]:
    """Marketing AND operating carrier codes across every segment."""
    carriers: set[str] = set()
    for seg in all_segments(offer):
        carriers.add(seg["carrierCode"])
        operating = seg.get("operating") or {}
        if operating.get("carrierCode"):
            carriers.add(operating["carrierCode"])
    return carriers


def carriers_ok(offer: Offer, blocked: list[str]) -> bool:
    """True if no segment is marketed or operated by a blocked carrier."""
    return not (offer_carriers(offer) & set(blocked))


def outbound_departure_ok(offer: Offer, earliest_hhmm: str) -> bool:
    """True if the OUTBOUND first segment departs at or after earliest_hhmm.

    Applies only to the outbound itinerary's first segment (local time);
    inbound and connection times are unconstrained.
    """
    departure_at = itinerary_segments(offer, 0)[0]["departure"]["at"]
    return departure_at[11:16] >= earliest_hhmm


def offer_hash(offer: Offer) -> str:
    """Stable dedupe key: same flights + dates + fare brands => same hash."""
    parts = [
        f'{seg["carrierCode"]}{seg["number"]}|{seg["departure"]["iataCode"]}|{seg["departure"]["at"]}'
        for seg in all_segments(offer)
    ]
    parts.extend(str(brand) for brand in fare_brand_names(offer))
    return hashlib.sha256("~".join(parts).encode()).hexdigest()[:16]


def evaluate_offer(
    offer: Offer, filters_config: dict[str, Any], earliest_departure: str
) -> str | None:
    """Run every filter; return None if the offer passes, else the first
    failing filter's name (for logging)."""
    if not carriers_ok(offer, filters_config["blocked_carriers"]):
        return "blocked_carrier"
    if not price_ok(offer, filters_config["max_price_usd"]):
        return "max_price"
    if not stops_ok(offer, filters_config["max_stops"]):
        return "max_stops"
    if not durations_ok(offer, filters_config["max_total_duration_hours"]):
        return "max_duration"
    if not connections_ok(offer, filters_config["min_connection_minutes"]):
        return "min_connection"
    if not fare_brands_ok(
        offer,
        filters_config["basic_economy_patterns"],
        filters_config.get("fare_brand_whitelist"),
    ):
        return "basic_economy"
    if not outbound_departure_ok(offer, earliest_departure):
        return "earliest_departure"
    return None
