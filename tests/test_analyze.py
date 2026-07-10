"""Tests for the read-only analysis reports."""
from __future__ import annotations

import json

import analyze

PATTERNS = ["BASIC", "LIGHT", "SAVER", "ECO BASIC"]


def rej(origin="GSO", dest="DEN", reason="max_price", brands=("MAIN CABIN",)):
    return {"origin": origin, "dest": dest, "filter_reason": reason,
            "fare_brand_names": json.dumps(list(brands))}


def off(origin="GSO", dest="DEN"):
    return {"origin": origin, "dest": dest}


CONFIG = {
    "origins": [{"code": "GSO", "earliest_departure": "08:00"},
                {"code": "RDU", "earliest_departure": "10:00"}],
    "destinations": {
        "domestic": [{"code": "DEN", "name": "Denver", "date_windows": []}],
        "international": [{"code": "SCL", "name": "Santiago", "date_windows": []}],
    },
}


class TestTally:
    def test_counts_by_reason(self):
        rows = [rej(reason="max_price"), rej(reason="max_price"), rej(reason="basic_economy")]
        tally = analyze.tally_rejections(rows)
        assert tally["max_price"] == 2 and tally["basic_economy"] == 1

    def test_empty(self):
        assert analyze.tally_rejections([]) == {}


class TestRouteHealth:
    ROUTES = analyze.load_configured_routes(CONFIG)  # GSO/RDU x DEN/SCL = 4

    def test_configured_routes_are_the_cross_product(self):
        assert set(self.ROUTES) == {
            ("GSO", "DEN", "domestic"), ("RDU", "DEN", "domestic"),
            ("GSO", "SCL", "international"), ("RDU", "SCL", "international"),
        }

    def test_healthy_when_offers_passed(self):
        health = analyze.route_health([off("GSO", "DEN")], [], self.ROUTES)
        gso_den = next(h for h in health if (h["origin"], h["dest"]) == ("GSO", "DEN"))
        assert gso_den["status"] == "healthy" and gso_den["passed"] == 1

    def test_drop_candidate_when_only_rejections(self):
        health = analyze.route_health([], [rej("RDU", "SCL"), rej("RDU", "SCL")], self.ROUTES)
        rdu_scl = next(h for h in health if (h["origin"], h["dest"]) == ("RDU", "SCL"))
        assert rdu_scl["status"] == "drop_candidate" and rdu_scl["rejected"] == 2

    def test_no_data_when_route_absent(self):
        health = analyze.route_health([off("GSO", "DEN")], [], self.ROUTES)
        gso_scl = next(h for h in health if (h["origin"], h["dest"]) == ("GSO", "SCL"))
        assert gso_scl["status"] == "no_data"


class TestBasicEconomyCandidates:
    def test_named_brand_surfaces_with_pattern(self):
        rows = [rej(reason="basic_economy", brands=("ECONOMY LIGHT", "ECONOMY LIGHT"))]
        out = analyze.basic_economy_candidates(rows, PATTERNS)
        assert out["candidates"] == [
            {"brand": "ECONOMY LIGHT", "offers": 1, "matched_patterns": ["LIGHT"]}
        ]
        assert out["total_basic_rejections"] == 1

    def test_counts_offers_not_segments(self):
        # Same brand across 4 segments in one offer counts once.
        rows = [rej(reason="basic_economy", brands=("BLUE BASIC",) * 4)]
        out = analyze.basic_economy_candidates(rows, PATTERNS)
        assert out["candidates"][0]["offers"] == 1

    def test_ranked_by_offer_count(self):
        rows = [
            rej(reason="basic_economy", brands=("BLUE BASIC",)),
            rej(reason="basic_economy", brands=("BLUE BASIC",)),
            rej(reason="basic_economy", brands=("ECO SAVER",)),
        ]
        out = analyze.basic_economy_candidates(rows, PATTERNS)
        assert [c["brand"] for c in out["candidates"]] == ["BLUE BASIC", "ECO SAVER"]

    def test_missing_brand_counted_separately_not_as_candidate(self):
        rows = [rej(reason="basic_economy", brands=(None, None))]
        out = analyze.basic_economy_candidates(rows, PATTERNS)
        assert out["candidates"] == [] and out["missing_brand_offers"] == 1

    def test_ignores_non_basic_reasons(self):
        rows = [rej(reason="max_price", brands=("BASIC ECONOMY",))]
        out = analyze.basic_economy_candidates(rows, PATTERNS)
        assert out["candidates"] == [] and out["total_basic_rejections"] == 0


class TestReportRenders:
    def test_format_report_smoke(self):
        tally = analyze.tally_rejections([rej(reason="max_price")])
        health = analyze.route_health([], [rej("RDU", "SCL")],
                                      analyze.load_configured_routes(CONFIG))
        basic = analyze.basic_economy_candidates(
            [rej(reason="basic_economy", brands=("ECONOMY LIGHT",))], PATTERNS)
        text = analyze.format_report(7, tally, health, basic)
        assert "FILTER REJECTIONS" in text
        assert "drop candidate" in text.lower()
        assert "ECONOMY LIGHT" in text
