"""Tests for the pure ticket filters."""
from __future__ import annotations

import filters
from conftest import make_ticket

PATTERNS_ORIGINS = {"GSO": "08:00", "CLT": "09:00", "RDU": "10:00"}


class TestPrice:
    def test_under_cap_ok(self):
        assert filters.price_ok(make_ticket(price=999), 1000)

    def test_at_cap_ok(self):
        assert filters.price_ok(make_ticket(price=1000), 1000)

    def test_over_cap_rejected(self):
        assert not filters.price_ok(make_ticket(price=1001), 1000)


class TestStops:
    def test_nonstop_and_one_stop_ok(self):
        assert filters.stops_ok(make_ticket(transfers=0), 1)
        assert filters.stops_ok(make_ticket(transfers=1), 1)

    def test_two_stops_rejected(self):
        assert not filters.stops_ok(make_ticket(transfers=2), 1)

    def test_missing_transfers_treated_as_zero(self):
        t = make_ticket()
        del t["transfers"]
        assert filters.stops(t) == 0


class TestDepartureFloor:
    def test_at_or_after_floor_ok(self):
        assert filters.outbound_departure_ok(make_ticket(dep_hhmm="10:00"), "10:00")
        assert filters.outbound_departure_ok(make_ticket(dep_hhmm="14:30"), "10:00")

    def test_before_floor_rejected(self):
        assert not filters.outbound_departure_ok(make_ticket(dep_hhmm="09:59"), "10:00")

    def test_each_origin_has_its_own_floor(self):
        early = make_ticket(dep_hhmm="08:30")
        assert filters.outbound_departure_ok(early, "08:00")       # GSO
        assert not filters.outbound_departure_ok(early, "09:00")   # CLT


class TestBlockedCarriers:
    BLOCKED = ["NK", "F9", "G4"]

    def test_clean_airline_ok(self):
        assert filters.carriers_ok(make_ticket(airline="DL"), self.BLOCKED)

    def test_blocked_airline_rejected(self):
        assert not filters.carriers_ok(make_ticket(airline="NK"), self.BLOCKED)

    def test_missing_airline_rejected(self):
        t = make_ticket()
        t["airline"] = None
        assert not filters.carriers_ok(t, self.BLOCKED)


class TestOfferHash:
    def test_stable_for_same_ticket(self):
        assert filters.offer_hash(make_ticket()) == filters.offer_hash(make_ticket())

    def test_differs_by_date(self):
        a = filters.offer_hash(make_ticket(depart_date="2026-09-20"))
        b = filters.offer_hash(make_ticket(depart_date="2026-09-21"))
        assert a != b

    def test_differs_by_flight_number(self):
        a = filters.offer_hash(make_ticket(flight_number=1))
        b = filters.offer_hash(make_ticket(flight_number=2))
        assert a != b


class TestEvaluateOffer:
    def test_clean_ticket_passes(self, filters_config):
        assert filters.evaluate_offer(make_ticket(), filters_config, "10:00") is None

    def test_blocked_carrier_reported_first(self, filters_config):
        # A blocked, overpriced ticket reports blocked_carrier (checked first).
        t = make_ticket(airline="NK", price=5000)
        assert filters.evaluate_offer(t, filters_config, "10:00") == "blocked_carrier"

    def test_price_reason(self, filters_config):
        assert filters.evaluate_offer(make_ticket(price=2000), filters_config, "10:00") == "max_price"

    def test_stops_reason(self, filters_config):
        assert filters.evaluate_offer(make_ticket(transfers=3), filters_config, "10:00") == "max_stops"

    def test_departure_reason(self, filters_config):
        t = make_ticket(dep_hhmm="06:00")
        assert filters.evaluate_offer(t, filters_config, "10:00") == "earliest_departure"
