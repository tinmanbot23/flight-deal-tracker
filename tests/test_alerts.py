"""Alert formatting and 24h offer-hash dedupe tests (no network calls)."""
from __future__ import annotations

import alerts
import db
from conftest import make_offer_row


class TestFormatting:
    def test_airline_label_known_and_unknown(self):
        assert alerts.airline_label("DL") == "Delta (DL)"
        assert alerts.airline_label("ZZ") == "ZZ"
        assert alerts.airline_label(None) == "?"

    def test_stops_label(self):
        assert alerts.stops_label(0) == "nonstop"
        assert alerts.stops_label(1) == "1 stop"
        assert alerts.stops_label(2) == "2 stops"

    def test_date_range_same_month(self):
        assert alerts.fmt_date_range("2026-11-09", "2026-11-17") == "Nov 9-17"

    def test_date_range_cross_month(self):
        assert alerts.fmt_date_range("2026-11-28", "2026-12-05") == "Nov 28-Dec 5"

    def test_format_deal(self):
        deal = make_offer_row(origin="RDU", dest="DEN", price_usd=249.0, airline="DL",
                              flight_number="1103", stops=0, depart_date="2026-11-09",
                              return_date="2026-11-14")
        text = alerts.format_deal(deal)
        assert text == "RDU->DEN $249 · Delta (DL) 1103 · nonstop · Nov 9-14"


class TestThresholdDedupe:
    def test_below_threshold_not_realerted_within_window(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        ts = "2026-07-09T08:00:00Z"
        db.insert_offer(conn, make_offer_row(price_usd=500, offer_hash="h1", run_timestamp=ts))
        conn.commit()
        config = {"filters": {"max_price_usd": 1000},
                  "alerts": {"threshold_dedupe_hours": 24}}

        first = alerts.threshold_deals(conn, config)
        assert [d["offer_hash"] for d in first] == ["h1"]

        db.record_alert(conn, "h1")
        assert alerts.threshold_deals(conn, config) == []   # deduped

        conn.close()

    def test_at_or_above_threshold_excluded(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        ts = "2026-07-09T08:00:00Z"
        db.insert_offer(conn, make_offer_row(price_usd=1000, offer_hash="h2", run_timestamp=ts))
        conn.commit()
        config = {"filters": {"max_price_usd": 1000},
                  "alerts": {"threshold_dedupe_hours": 24}}
        assert alerts.threshold_deals(conn, config) == []
        conn.close()
