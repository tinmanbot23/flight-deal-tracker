"""Date sampling, destination rotation, and offer-row construction tests."""
from __future__ import annotations

import json
import math
from datetime import date

import yaml

import db
import tracker


class TestSampleDates:
    def test_three_evenly_spaced(self):
        dates = tracker.sample_dates(date(2026, 9, 1), date(2026, 9, 30), 3)
        assert dates == [date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 30)]

    def test_single_date_is_midpoint(self):
        assert tracker.sample_dates(date(2026, 9, 1), date(2026, 9, 30), 1) == [date(2026, 9, 15)]

    def test_clamped_to_not_before(self):
        dates = tracker.sample_dates(
            date(2026, 6, 1), date(2026, 9, 30), 2, not_before=date(2026, 7, 10)
        )
        assert dates == [date(2026, 7, 10), date(2026, 9, 30)]

    def test_window_fully_past_returns_empty(self):
        assert tracker.sample_dates(
            date(2026, 5, 1), date(2026, 6, 1), 3, not_before=date(2026, 7, 10)
        ) == []

    def test_narrow_window_dedupes(self):
        dates = tracker.sample_dates(date(2026, 9, 1), date(2026, 9, 2), 3)
        assert dates == [date(2026, 9, 1), date(2026, 9, 2)]


class TestRotation:
    ITEMS = [f"D{i}" for i in range(15)]

    def test_slice_size(self):
        assert len(tracker.rotate(self.ITEMS, 2, 0)) == 2

    def test_deterministic(self):
        assert tracker.rotate(self.ITEMS, 2, 7) == tracker.rotate(self.ITEMS, 2, 7)

    def test_every_destination_covered_over_a_cycle(self):
        per_run = 2
        cycle = math.ceil(len(self.ITEMS) / per_run) + 1
        covered = set()
        for run_index in range(cycle * 2):
            covered.update(tracker.rotate(self.ITEMS, per_run, run_index))
        assert covered == set(self.ITEMS)

    def test_per_run_larger_than_list_returns_all(self):
        assert tracker.rotate(["A", "B"], 5, 3) == ["A", "B"]


class TestBuildTasks:
    CONFIG = {
        "origins": [
            {"code": "GSO", "earliest_departure": "08:00"},
            {"code": "RDU", "earliest_departure": "10:00"},
        ],
        "destinations": {
            "domestic": [
                {"code": "DEN", "name": "Denver", "date_windows": [
                    {"depart_start": "2026-09-01", "depart_end": "2026-09-30",
                     "trip_length_days": 5},
                ]},
            ],
            "international": [
                {"code": "SCL", "name": "Santiago", "date_windows": [
                    {"depart_start": "2026-11-01", "depart_end": "2026-11-30",
                     "trip_length_days": 8},
                ]},
            ],
        },
        "rotation": {"domestic_per_run": 1, "international_per_run": 1,
                     "windows_per_run": 1, "dates_per_window": 2},
    }

    def test_call_count_matches_budget_formula(self):
        tasks = tracker.build_tasks(self.CONFIG, 0, date(2026, 7, 9))
        # 2 origins x 2 destinations x 1 window x 2 dates
        assert len(tasks) == 8

    def test_return_date_uses_trip_length(self):
        tasks = tracker.build_tasks(self.CONFIG, 0, date(2026, 7, 9))
        den = next(t for t in tasks if t["dest"] == "DEN")
        assert (date.fromisoformat(den["return_date"])
                - date.fromisoformat(den["depart_date"])).days == 5

    def test_origin_carries_its_own_departure_floor(self):
        tasks = tracker.build_tasks(self.CONFIG, 0, date(2026, 7, 9))
        floors = {t["origin"]: t["earliest_departure"] for t in tasks}
        assert floors == {"GSO": "08:00", "RDU": "10:00"}


class TestBuildOfferRow:
    def test_row_fields(self, good_offer, response):
        row = tracker.build_offer_row(
            good_offer, response["dictionaries"],
            run_timestamp="2026-07-09T08:00:00Z", origin="RDU", dest="SCL",
            dest_region="international", depart_date="2026-11-09",
            return_date="2026-11-17",
        )
        assert row["price_usd"] == 842.0
        assert row["stops_outbound"] == 1 and row["stops_inbound"] == 1
        assert json.loads(row["connection_airports"]) == ["ATL", "ATL"]
        assert row["total_duration_minutes"] == 750 + 787
        assert json.loads(row["fare_brand_names"]) == ["MAIN CABIN"] * 4

        outbound = json.loads(row["outbound_json"])
        assert outbound[0]["flight_number"] == "DL1103"
        assert outbound[0]["carrier_name"] == "DELTA AIR LINES"
        assert outbound[1]["aircraft"] == "AIRBUS A350-900"
        assert outbound[0]["depart_time"] == "2026-11-09T11:40:00"


class _ExplodingClient:
    """Constructing this means the budget guard failed to stop the run."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("AmadeusClient constructed despite budget guard")


class _EmptyClient:
    """A client that makes calls (recording each) but returns no offers."""

    def __init__(self, *args, on_call=None, **kwargs):
        self.calls_made = 0
        self._on_call = on_call

    def search_flight_offers(self, **kwargs):
        self.calls_made += 1
        if self._on_call:
            self._on_call()
        return {"data": [], "dictionaries": {}}


class TestBudgetGuard:
    """The monthly api_budget guard: skips the run before any auth/network
    when the month's used calls plus this run's planned calls exceed the cap."""

    BASE_CONFIG = {
        "origins": [
            {"code": "GSO", "earliest_departure": "08:00"},
            {"code": "RDU", "earliest_departure": "10:00"},
        ],
        "destinations": {
            "domestic": [{"code": "DEN", "name": "Denver", "date_windows": [
                {"depart_start": "2026-09-01", "depart_end": "2026-09-30",
                 "trip_length_days": 5}]}],
            "international": [{"code": "SCL", "name": "Santiago", "date_windows": [
                {"depart_start": "2026-11-01", "depart_end": "2026-11-30",
                 "trip_length_days": 8}]}],
        },
        "rotation": {"domestic_per_run": 1, "international_per_run": 1,
                     "windows_per_run": 1, "dates_per_window": 2},
        "search": {"adults": 1, "currency": "USD", "max_results": 20},
        "filters": {
            "max_price_usd": 1000, "max_stops": 1, "max_total_duration_hours": 14,
            "min_connection_minutes": 60, "cabin": "ECONOMY",
            "basic_economy_patterns": ["BASIC"], "blocked_carriers": ["NK"],
        },
    }
    PLANNED = 8  # 2 origins x 2 destinations x 1 window x 2 dates

    def _write(self, tmp_path, max_calls):
        config = dict(self.BASE_CONFIG)
        config["api_budget"] = {"monthly_max_calls": max_calls}
        path = tmp_path / "config.yml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return str(path)

    def test_planned_matches_formula(self):
        assert len(tracker.build_tasks(
            {**self.BASE_CONFIG, "api_budget": {"monthly_max_calls": 1}}, 0, date(2026, 7, 9)
        )) == self.PLANNED

    def test_guard_trips_before_any_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tracker, "AmadeusClient", _ExplodingClient)
        config_path = self._write(tmp_path, max_calls=self.PLANNED - 1)
        db_path = str(tmp_path / "t.db")

        rc = tracker.run(config_path=config_path, db_path=db_path)

        assert rc == 0
        conn = db.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        assert db.get_month_calls(conn) == 0
        conn.close()

    def test_guard_allows_when_budget_sufficient(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tracker, "AmadeusClient", _EmptyClient)
        config_path = self._write(tmp_path, max_calls=self.PLANNED)  # boundary: == is allowed
        db_path = str(tmp_path / "t.db")

        rc = tracker.run(config_path=config_path, db_path=db_path)

        assert rc == 0
        conn = db.connect(db_path)
        assert db.get_month_calls(conn) == self.PLANNED
        conn.close()

    def test_accumulated_usage_trips_guard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tracker, "AmadeusClient", _ExplodingClient)
        config_path = self._write(tmp_path, max_calls=2500)
        db_path = str(tmp_path / "t.db")
        conn = db.connect(db_path)
        db.add_api_calls(conn, 2495)  # earlier runs this month
        conn.close()

        rc = tracker.run(config_path=config_path, db_path=db_path)

        assert rc == 0  # 2495 + 8 > 2500, so skipped
        conn = db.connect(db_path)
        assert db.get_month_calls(conn) == 2495  # unchanged
        conn.close()


def _fixed_offers_client(offers):
    """Build a client class that returns the same offers for every search."""
    class _Client:
        def __init__(self, *args, on_call=None, **kwargs):
            self.calls_made = 0
            self._on_call = on_call

        def search_flight_offers(self, **kwargs):
            self.calls_made += 1
            if self._on_call:
                self._on_call()
            return {"data": offers, "dictionaries": {"carriers": {}, "aircraft": {}}}
    return _Client


class TestRejectionInstrumentation:
    """A rejected offer is recorded in the rejections table with its reason,
    fare brands, and carriers; a passing offer still lands in offers."""

    def _config(self, tmp_path):
        config = dict(TestBudgetGuard.BASE_CONFIG)
        config["api_budget"] = {"monthly_max_calls": 2500}
        path = tmp_path / "c.yml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return str(path)

    def test_rejections_recorded_with_detail(
        self, tmp_path, monkeypatch, good_offer, basic_offer
    ):
        import copy
        blocked = copy.deepcopy(good_offer)
        blocked["itineraries"][0]["segments"][0]["carrierCode"] = "NK"
        offers = [good_offer, basic_offer, blocked]
        monkeypatch.setattr(tracker, "AmadeusClient", _fixed_offers_client(offers))

        db_path = str(tmp_path / "t.db")
        tracker.run(config_path=self._config(tmp_path), db_path=db_path)

        conn = db.connect(db_path)
        reasons = {r["filter_reason"] for r in conn.execute("SELECT filter_reason FROM rejections")}
        assert "basic_economy" in reasons
        assert "blocked_carrier" in reasons

        basic_row = conn.execute(
            "SELECT fare_brand_names, carriers FROM rejections "
            "WHERE filter_reason='basic_economy' LIMIT 1"
        ).fetchone()
        assert "BASIC ECONOMY" in basic_row["fare_brand_names"]
        assert "DL" in basic_row["carriers"]

        blocked_row = conn.execute(
            "SELECT carriers FROM rejections WHERE filter_reason='blocked_carrier' LIMIT 1"
        ).fetchone()
        assert "NK" in blocked_row["carriers"]

        # The good offer still passed and was stored.
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] >= 1
        conn.close()

    def test_malformed_offer_does_not_crash_or_record(self, tmp_path, monkeypatch):
        # An offer missing 'itineraries' throws inside evaluate_offer and is
        # skipped (logged), not recorded as a rejection.
        monkeypatch.setattr(tracker, "AmadeusClient", _fixed_offers_client([{"price": {}}]))
        db_path = str(tmp_path / "t.db")
        rc = tracker.run(config_path=self._config(tmp_path), db_path=db_path)
        assert rc == 0
        conn = db.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        conn.close()
