"""Tests for months/rotation, task building, budget guard, and rejection
instrumentation in the Travelpayouts tracker."""
from __future__ import annotations

from datetime import date

import yaml

import db
import tracker
from conftest import make_ticket


class TestMonthsInWindow:
    def test_single_month(self):
        assert tracker.months_in_window(date(2026, 9, 5), date(2026, 9, 30)) == ["2026-09"]

    def test_spanning_year_boundary(self):
        assert tracker.months_in_window(date(2026, 11, 20), date(2027, 1, 10)) == [
            "2026-11", "2026-12", "2027-01"
        ]


class TestRotate:
    def test_wraps_and_covers(self):
        items = ["A", "B", "C", "D", "E"]
        seen = set()
        for i in range(5):
            seen.update(tracker.rotate(items, 2, i))
        assert seen == set(items)

    def test_per_run_ge_length_returns_all(self):
        assert tracker.rotate(["A", "B"], 5, 3) == ["A", "B"]


CONFIG = {
    "origins": [
        {"code": "GSO", "earliest_departure": "08:00"},
        {"code": "RDU", "earliest_departure": "10:00"},
    ],
    "destinations": {
        "domestic": [{"code": "DEN", "name": "Denver", "date_windows": [
            {"depart_start": "2026-09-01", "depart_end": "2026-10-31", "trip_length_days": 5}]}],
        "international": [{"code": "SCL", "name": "Santiago", "date_windows": [
            {"depart_start": "2026-11-01", "depart_end": "2026-11-30", "trip_length_days": 8}]}],
    },
    "rotation": {"domestic_per_run": 1, "international_per_run": 1, "windows_per_run": 1},
    "search": {"currency": "usd"},
    "filters": {"max_price_usd": 1000, "max_stops": 1, "blocked_carriers": ["NK"]},
}


class TestBuildTasks:
    def test_call_count_is_origins_x_dests_x_months(self):
        tasks = tracker.build_tasks(CONFIG, 0, date(2026, 7, 9))
        # 2 origins x (DEN: 2 months + SCL: 1 month) = 2 x 3 = 6 calls.
        assert len(tasks) == 6

    def test_task_carries_month_length_and_floor(self):
        tasks = tracker.build_tasks(CONFIG, 0, date(2026, 7, 9))
        den = next(t for t in tasks if t["dest"] == "DEN" and t["origin"] == "RDU")
        assert den["length"] == 5 and den["earliest_departure"] == "10:00"
        assert den["month"] in {"2026-09", "2026-10"}


class TestBuildRows:
    def test_offer_row_fields(self):
        t = make_ticket(origin="RDU", destination="DEN", price=249, transfers=1,
                        airline="DL", flight_number=1103, depart_date="2026-09-20", length=5)
        row = tracker.build_offer_row(
            t, "2026-09-20", run_timestamp="2026-07-09T08:00:00Z",
            origin="RDU", dest="DEN", dest_region="domestic", currency="usd")
        assert row["price_usd"] == 249.0
        assert row["return_date"] == "2026-09-25"
        assert row["airline"] == "DL" and row["flight_number"] == "1103"
        assert row["stops"] == 1 and row["currency"] == "USD"

    def test_rejection_row_is_defensive(self):
        row = tracker.build_rejection_row(
            {"airline": "NK"}, "2026-09-20", run_timestamp="t",
            origin="RDU", dest="DEN", dest_region="domestic", reason="blocked_carrier")
        assert row["filter_reason"] == "blocked_carrier"
        assert row["airline"] == "NK"
        assert row["price_usd"] is None  # missing price didn't crash


class _ExplodingClient:
    def __init__(self, *a, **k):
        raise AssertionError("client constructed despite budget guard")


def _calendar_client(ticket_factory):
    """Client whose prices_calendar builds tickets for each day of the month."""
    import calendar as _cal

    class _Client:
        def __init__(self, *a, on_call=None, **k):
            self.calls_made = 0
            self._on_call = on_call

        def prices_calendar(self, *, origin, destination, depart_month, length, currency=None):
            self.calls_made += 1
            if self._on_call:
                self._on_call()
            year, month = (int(x) for x in depart_month.split("-"))
            days = _cal.monthrange(year, month)[1]
            out = {}
            for day in range(1, days + 1):
                d = f"{year:04d}-{month:02d}-{day:02d}"
                out[d] = ticket_factory(origin, destination, d, length, day)
            return out
    return _Client


class TestBudgetGuard:
    PLANNED = 6

    def _write(self, tmp_path, max_calls):
        config = dict(CONFIG)
        config["api_budget"] = {"monthly_max_calls": max_calls}
        path = tmp_path / "config.yml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return str(path)

    def test_guard_trips_before_any_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tracker, "TravelpayoutsClient", _ExplodingClient)
        rc = tracker.run(config_path=self._write(tmp_path, self.PLANNED - 1),
                         db_path=str(tmp_path / "t.db"))
        assert rc == 0
        conn = db.connect(str(tmp_path / "t.db"))
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        assert db.get_month_calls(conn) == 0
        conn.close()

    def test_accumulated_usage_trips_guard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tracker, "TravelpayoutsClient", _ExplodingClient)
        db_path = str(tmp_path / "t.db")
        conn = db.connect(db_path)
        db.add_api_calls(conn, 9999)
        conn.close()
        rc = tracker.run(config_path=self._write(tmp_path, 10000), db_path=db_path)
        assert rc == 0  # 9999 + 6 > 10000
        conn = db.connect(db_path)
        assert db.get_month_calls(conn) == 9999
        conn.close()


class TestRunFiltersAndInstruments:
    def _config(self, tmp_path, **window):
        config = dict(CONFIG)
        config["api_budget"] = {"monthly_max_calls": 10000}
        # Single near-future window we control.
        config["destinations"] = {
            "domestic": [{"code": "DEN", "name": "Denver", "date_windows": [window]}],
            "international": [],
        }
        config["rotation"] = {"domestic_per_run": 1, "international_per_run": 0,
                              "windows_per_run": 1}
        path = tmp_path / "c.yml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return str(path)

    def test_bad_tickets_recorded_as_rejections(self, tmp_path, monkeypatch):
        def factory(origin, dest, d, length, day):
            r = day % 4
            if r == 0:
                return make_ticket(origin=origin, destination=dest, depart_date=d, airline="NK")
            if r == 1:
                return make_ticket(origin=origin, destination=dest, depart_date=d, price=1500)
            if r == 2:
                return make_ticket(origin=origin, destination=dest, depart_date=d, transfers=2)
            return make_ticket(origin=origin, destination=dest, depart_date=d, price=300, transfers=1)

        monkeypatch.setattr(tracker, "TravelpayoutsClient", _calendar_client(factory))
        cfg = self._config(tmp_path, depart_start="2026-09-01", depart_end="2026-09-30",
                           trip_length_days=5)
        db_path = str(tmp_path / "t.db")
        tracker.run(config_path=cfg, db_path=db_path)

        conn = db.connect(db_path)
        reasons = {r["filter_reason"] for r in conn.execute("SELECT filter_reason FROM rejections")}
        assert {"blocked_carrier", "max_price", "max_stops"} <= reasons
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] > 0
        conn.close()

    def test_only_dates_inside_window_stored(self, tmp_path, monkeypatch):
        def factory(origin, dest, d, length, day):
            return make_ticket(origin=origin, destination=dest, depart_date=d, price=300, transfers=0)

        monkeypatch.setattr(tracker, "TravelpayoutsClient", _calendar_client(factory))
        # Window is only three days, though the calendar returns the whole month.
        cfg = self._config(tmp_path, depart_start="2026-09-10", depart_end="2026-09-12",
                           trip_length_days=5)
        db_path = str(tmp_path / "t.db")
        tracker.run(config_path=cfg, db_path=db_path)

        conn = db.connect(db_path)
        dates = {r["depart_date"] for r in conn.execute("SELECT depart_date FROM offers")}
        conn.close()
        # Only the three in-window dates are stored, though the calendar month
        # returned ~30 days (both origins contribute rows for each date).
        assert dates == {"2026-09-10", "2026-09-11", "2026-09-12"}
