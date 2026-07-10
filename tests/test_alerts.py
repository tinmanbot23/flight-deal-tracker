"""Alert formatting and 24h offer-hash dedupe tests (no network calls)."""
from __future__ import annotations

import json

import pytest

import alerts
import db
from conftest import make_row

CONFIG = {
    "filters": {"max_price_usd": 1000},
    "ranking": {"history_days": 30, "min_history_offers": 3},
    "alerts": {"threshold_dedupe_hours": 24, "max_threshold_alerts_per_run": 5},
}


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


class TestFormatting:
    def test_clock(self):
        assert alerts.fmt_clock("2026-11-09T11:40:00") == "11:40a"
        assert alerts.fmt_clock("2026-11-09T15:55:00") == "3:55p"
        assert alerts.fmt_clock("2026-11-10T00:10:00") == "12:10a"

    def test_date_range_same_month(self):
        assert alerts.fmt_date_range("2026-11-09", "2026-11-17") == "Nov 9–17"

    def test_date_range_cross_month(self):
        assert alerts.fmt_date_range("2026-11-28", "2026-12-05") == "Nov 28–Dec 5"

    def test_leg_marks_overnight_arrival(self):
        segments = [
            {"flight_number": "DL1103", "depart_airport": "RDU",
             "depart_time": "2026-11-09T11:40:00",
             "arrive_airport": "ATL", "arrive_time": "2026-11-09T13:05:00"},
            {"flight_number": "DL147", "depart_airport": "ATL",
             "depart_time": "2026-11-09T15:55:00",
             "arrive_airport": "SCL", "arrive_time": "2026-11-10T05:10:00"},
        ]
        assert alerts.fmt_leg(segments) == (
            "DL1103 RDU 11:40a→ATL 1:05p, DL147 ATL 3:55p→SCL 5:10a+1"
        )

    def test_format_deal_is_bookable_summary(self):
        row = make_row()
        deal = dict(row)
        deal["outbound"] = json.loads(deal.pop("outbound_json"))
        deal["inbound"] = json.loads(deal.pop("inbound_json"))
        deal["fare_brands"] = json.loads(deal.pop("fare_brand_names"))
        text = alerts.format_deal(deal)
        assert text.startswith("RDU→SCL $842 · Delta Air Lines DL1103 RDU 11:40a→ATL 1:05p")
        assert "Main Cabin" in text
        assert text.endswith("Nov 9–17")


class TestDedupe:
    def test_recent_alert_detected(self, conn):
        db.record_alert(conn, "hash-1")
        assert db.was_alerted_recently(conn, "hash-1", 24)

    def test_unknown_hash_not_deduped(self, conn):
        assert not db.was_alerted_recently(conn, "never-seen", 24)

    def test_old_alert_expires(self, conn):
        conn.execute(
            "INSERT INTO alerts (offer_hash, alerted_at) VALUES (?, ?)",
            ("hash-old", "2026-07-01T00:00:00Z"),
        )
        conn.commit()
        assert not db.was_alerted_recently(conn, "hash-old", 24)


class TestThresholdDeals:
    def test_below_threshold_and_unalerted_is_returned(self, conn):
        db.insert_offer(conn, make_row(price_usd=842, offer_hash="cheap"))
        conn.commit()
        deals = alerts.threshold_deals(conn, CONFIG)
        assert [d["offer_hash"] for d in deals] == ["cheap"]

    def test_recently_alerted_hash_is_skipped(self, conn):
        db.insert_offer(conn, make_row(price_usd=842, offer_hash="cheap"))
        conn.commit()
        db.record_alert(conn, "cheap")
        assert alerts.threshold_deals(conn, CONFIG) == []

    def test_at_or_above_threshold_is_excluded(self, conn):
        db.insert_offer(conn, make_row(price_usd=1000, offer_hash="at-cap"))
        conn.commit()
        assert alerts.threshold_deals(conn, CONFIG) == []

    def test_only_cheapest_per_route(self, conn):
        db.insert_offer(conn, make_row(price_usd=842, offer_hash="a"))
        db.insert_offer(conn, make_row(price_usd=790, offer_hash="b"))
        conn.commit()
        deals = alerts.threshold_deals(conn, CONFIG)
        assert [d["offer_hash"] for d in deals] == ["b"]

    def test_log_only_when_pushover_unset(self, monkeypatch):
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        assert alerts.send_pushover("title", "message") is False
