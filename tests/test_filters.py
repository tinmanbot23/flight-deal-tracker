"""Unit tests for every itinerary filter, against real Amadeus response shape."""
from __future__ import annotations

import pytest

import filters


class TestParseDuration:
    def test_hours_and_minutes(self):
        assert filters.parse_duration_minutes("PT13H30M") == 810

    def test_hours_only(self):
        assert filters.parse_duration_minutes("PT2H") == 120

    def test_minutes_only(self):
        assert filters.parse_duration_minutes("PT45M") == 45

    def test_with_days(self):
        assert filters.parse_duration_minutes("P1DT2H") == 26 * 60

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            filters.parse_duration_minutes("13 hours")


class TestPrice:
    def test_under_cap_passes(self, good_offer, filters_config):
        assert filters.price_ok(good_offer, 1000)

    def test_over_cap_fails(self, good_offer):
        good_offer["price"]["grandTotal"] = "1042.00"
        assert not filters.price_ok(good_offer, 1000)


class TestStops:
    def test_one_stop_passes(self, good_offer):
        assert filters.itinerary_stops(good_offer, 0) == 1
        assert filters.stops_ok(good_offer, 1)

    def test_two_stops_fails(self, good_offer):
        segs = good_offer["itineraries"][0]["segments"]
        segs.append(dict(segs[-1]))
        assert not filters.stops_ok(good_offer, 1)


class TestDuration:
    def test_each_direction_under_limit(self, good_offer):
        assert filters.durations_ok(good_offer, 14)

    def test_one_direction_over_limit_fails(self, good_offer):
        good_offer["itineraries"][1]["duration"] = "PT15H10M"
        assert not filters.durations_ok(good_offer, 14)

    def test_total_duration_sums_both_directions(self, good_offer):
        assert filters.total_duration_minutes(good_offer) == 750 + 787


class TestConnections:
    def test_gaps_computed(self, good_offer):
        assert filters.connection_gaps_minutes(good_offer) == [85, 150]

    def test_minimum_met(self, good_offer):
        assert filters.connections_ok(good_offer, 60)

    def test_short_connection_fails(self, good_offer):
        # Shrink the ATL outbound connection to 40 minutes.
        good_offer["itineraries"][0]["segments"][1]["departure"]["at"] = "2026-11-09T13:45:00"
        assert not filters.connections_ok(good_offer, 60)

    def test_connection_airports(self, good_offer):
        assert filters.connection_airports(good_offer) == ["ATL", "ATL"]


class TestBasicEconomy:
    PATTERNS = ["BASIC", "LIGHT", "SAVER", "ECO BASIC"]

    @pytest.mark.parametrize("brand", [
        "BASIC ECONOMY", "Basic", "LIGHT", "Eco Light", "SAVER FARE", "ECO BASIC",
    ])
    def test_basic_patterns_rejected(self, brand):
        assert filters.is_basic_economy(brand, self.PATTERNS)

    @pytest.mark.parametrize("brand", ["MAIN CABIN", "COMFORT+", "ECONOMY FLEX"])
    def test_real_brands_accepted(self, brand):
        assert not filters.is_basic_economy(brand, self.PATTERNS)

    @pytest.mark.parametrize("brand", [None, "", "   "])
    def test_missing_brand_rejected_when_in_doubt(self, brand):
        assert filters.is_basic_economy(brand, self.PATTERNS)

    def test_good_offer_passes(self, good_offer, filters_config):
        assert filters.fare_brands_ok(good_offer, filters_config["basic_economy_patterns"])

    def test_basic_offer_rejected(self, basic_offer, filters_config):
        assert not filters.fare_brands_ok(basic_offer, filters_config["basic_economy_patterns"])

    def test_one_unbranded_segment_rejects_whole_offer(self, good_offer, filters_config):
        details = good_offer["travelerPricings"][0]["fareDetailsBySegment"]
        del details[2]["brandedFare"], details[2]["brandedFareLabel"]
        assert not filters.fare_brands_ok(good_offer, filters_config["basic_economy_patterns"])

    def test_missing_fare_details_padded_and_rejected(self, good_offer, filters_config):
        good_offer["travelerPricings"][0]["fareDetailsBySegment"] = []
        assert filters.fare_brand_names(good_offer) == [None] * 4
        assert not filters.fare_brands_ok(good_offer, filters_config["basic_economy_patterns"])


class TestFareBrandWhitelist:
    PATTERNS = ["BASIC", "LIGHT", "SAVER", "ECO BASIC"]

    def test_whitelisted_brand_exempted_from_pattern(self):
        # "ECONOMY LIGHT PLUS" contains LIGHT but is whitelisted.
        assert filters.is_basic_economy("ECONOMY LIGHT PLUS", self.PATTERNS)
        assert not filters.is_basic_economy(
            "ECONOMY LIGHT PLUS", self.PATTERNS, ["Economy Light Plus"]
        )

    def test_whitelist_is_exact_match_not_substring(self):
        # Whitelisting the full name must not exempt a different LIGHT fare.
        assert filters.is_basic_economy("ECONOMY LIGHT", self.PATTERNS, ["Economy Light Plus"])

    def test_whitelist_case_insensitive(self):
        assert not filters.is_basic_economy("eco basic go", self.PATTERNS, ["ECO BASIC GO"])

    def test_missing_brand_still_rejected_despite_whitelist(self):
        assert filters.is_basic_economy(None, self.PATTERNS, ["anything"])
        assert filters.is_basic_economy("  ", self.PATTERNS, ["anything"])

    def test_fare_brands_ok_threads_whitelist(self, good_offer):
        details = good_offer["travelerPricings"][0]["fareDetailsBySegment"]
        for d in details:
            d["brandedFareLabel"] = "ECONOMY LIGHT PLUS"
        assert not filters.fare_brands_ok(good_offer, self.PATTERNS)
        assert filters.fare_brands_ok(good_offer, self.PATTERNS, ["ECONOMY LIGHT PLUS"])

    def test_evaluate_offer_respects_whitelist(self, good_offer, filters_config):
        details = good_offer["travelerPricings"][0]["fareDetailsBySegment"]
        for d in details:
            d["brandedFareLabel"] = "ECONOMY LIGHT PLUS"
        assert filters.evaluate_offer(good_offer, filters_config, "10:00") == "basic_economy"
        whitelisted = {**filters_config, "fare_brand_whitelist": ["ECONOMY LIGHT PLUS"]}
        assert filters.evaluate_offer(good_offer, whitelisted, "10:00") is None


class TestBlockedCarriers:
    BLOCKED = ["NK", "F9", "G4", "XP", "SY", "OG", "N0", "MX", "Y4", "VB"]

    def test_clean_offer_passes(self, good_offer):
        assert filters.carriers_ok(good_offer, self.BLOCKED)

    def test_marketing_carrier_blocked(self, good_offer):
        good_offer["itineraries"][0]["segments"][0]["carrierCode"] = "NK"
        assert not filters.carriers_ok(good_offer, self.BLOCKED)

    def test_operating_carrier_blocked(self, good_offer):
        # Marketed by DL but operated by Frontier on one segment.
        good_offer["itineraries"][1]["segments"][1]["operating"] = {"carrierCode": "F9"}
        assert not filters.carriers_ok(good_offer, self.BLOCKED)


class TestDepartureFloor:
    def test_1140_departure_meets_gso_floor(self, good_offer):
        assert filters.outbound_departure_ok(good_offer, "08:00")

    def test_1140_departure_meets_rdu_floor(self, good_offer):
        assert filters.outbound_departure_ok(good_offer, "10:00")

    def test_early_departure_fails_floor(self, good_offer):
        good_offer["itineraries"][0]["segments"][0]["departure"]["at"] = "2026-11-09T07:30:00"
        assert not filters.outbound_departure_ok(good_offer, "08:00")

    def test_floor_ignores_inbound_and_connections(self, good_offer):
        # Early inbound departure must not trip the outbound-only floor.
        good_offer["itineraries"][1]["segments"][0]["departure"]["at"] = "2026-11-17T05:05:00"
        assert filters.outbound_departure_ok(good_offer, "10:00")


class TestOfferHash:
    def test_stable(self, good_offer):
        import copy
        assert filters.offer_hash(good_offer) == filters.offer_hash(copy.deepcopy(good_offer))

    def test_differs_by_fare_brand(self, good_offer, basic_offer):
        # Same flights, different brand => different hash.
        assert filters.offer_hash(good_offer) != filters.offer_hash(basic_offer)

    def test_differs_by_date(self, good_offer):
        import copy
        moved = copy.deepcopy(good_offer)
        for seg in filters.all_segments(moved):
            seg["departure"]["at"] = seg["departure"]["at"].replace("-11-", "-12-")
        assert filters.offer_hash(good_offer) != filters.offer_hash(moved)


class TestEvaluateOffer:
    def test_good_offer_passes_all(self, good_offer, filters_config):
        assert filters.evaluate_offer(good_offer, filters_config, "10:00") is None

    def test_reports_first_failing_filter(self, basic_offer, filters_config):
        assert filters.evaluate_offer(basic_offer, filters_config, "10:00") == "basic_economy"

    def test_departure_floor_enforced_per_origin(self, good_offer, filters_config):
        assert filters.evaluate_offer(good_offer, filters_config, "12:00") == "earliest_departure"
