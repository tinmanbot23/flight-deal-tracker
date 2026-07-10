"""Tests for the read-only analysis reports."""
from __future__ import annotations

import analyze


def rej(origin="GSO", dest="DEN", reason="max_price"):
    return {"origin": origin, "dest": dest, "filter_reason": reason}


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
        rows = [rej(reason="max_price"), rej(reason="max_price"), rej(reason="blocked_carrier")]
        tally = analyze.tally_rejections(rows)
        assert tally["max_price"] == 2 and tally["blocked_carrier"] == 1

    def test_empty(self):
        assert analyze.tally_rejections([]) == {}


class TestRouteHealth:
    ROUTES = analyze.load_configured_routes(CONFIG)  # GSO/RDU x DEN/SCL = 4

    def test_cross_product(self):
        assert set(self.ROUTES) == {
            ("GSO", "DEN", "domestic"), ("RDU", "DEN", "domestic"),
            ("GSO", "SCL", "international"), ("RDU", "SCL", "international"),
        }

    def test_healthy_when_offers_passed(self):
        health = analyze.route_health([off("GSO", "DEN")], [], self.ROUTES)
        h = next(x for x in health if (x["origin"], x["dest"]) == ("GSO", "DEN"))
        assert h["status"] == "healthy"

    def test_drop_candidate_when_only_rejections(self):
        health = analyze.route_health([], [rej("RDU", "SCL")], self.ROUTES)
        h = next(x for x in health if (x["origin"], x["dest"]) == ("RDU", "SCL"))
        assert h["status"] == "drop_candidate"

    def test_no_data_when_route_absent(self):
        health = analyze.route_health([off("GSO", "DEN")], [], self.ROUTES)
        h = next(x for x in health if (x["origin"], x["dest"]) == ("GSO", "SCL"))
        assert h["status"] == "no_data"


class TestReport:
    def test_format_report_smoke(self):
        tally = analyze.tally_rejections([rej(reason="max_price")])
        health = analyze.route_health([], [rej("RDU", "SCL")],
                                      analyze.load_configured_routes(CONFIG))
        text = analyze.format_report(7, tally, health)
        assert "FILTER REJECTIONS" in text
        assert "drop candidate" in text.lower()
