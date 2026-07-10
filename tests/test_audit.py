"""Tests for the post-storage filter-leak audit."""
from __future__ import annotations

import audit
import db
from conftest import make_offer_row

FILTERS = {"max_price_usd": 1000, "max_stops": 1, "blocked_carriers": ["NK", "F9"]}
ORIGINS = {"GSO": "08:00", "CLT": "09:00", "RDU": "10:00"}


class TestAuditRow:
    def test_clean_offer_no_violations(self):
        assert audit.audit_row(make_offer_row(), FILTERS, ORIGINS) == []

    def test_overpriced_caught(self):
        v = audit.audit_row(make_offer_row(price_usd=1200), FILTERS, ORIGINS)
        assert any("price" in x for x in v)

    def test_too_many_stops_caught(self):
        v = audit.audit_row(make_offer_row(stops=2), FILTERS, ORIGINS)
        assert any("stops" in x for x in v)

    def test_blocked_carrier_caught(self):
        v = audit.audit_row(make_offer_row(airline="NK"), FILTERS, ORIGINS)
        assert any("blocked carrier" in x for x in v)

    def test_departure_floor_caught(self):
        # RDU floor is 10:00; 09:30 departure violates it.
        row = make_offer_row(origin="RDU", departure_at="2026-09-20T09:30:00Z")
        v = audit.audit_row(row, FILTERS, ORIGINS)
        assert any("floor" in x for x in v)


class TestAuditRun:
    def test_clean_run_reports_no_leaks(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        db.insert_offer(conn, make_offer_row(offer_hash="clean1"))
        conn.commit()
        config = {"filters": FILTERS, "origins": [{"code": "RDU", "earliest_departure": "10:00"}]}
        assert audit.audit_run(conn, config) == {}
        conn.close()

    def test_leaky_row_flagged(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        db.insert_offer(conn, make_offer_row(offer_hash="bad1", price_usd=1500))
        conn.commit()
        config = {"filters": FILTERS, "origins": [{"code": "RDU", "earliest_departure": "10:00"}]}
        leaks = audit.audit_run(conn, config)
        assert "bad1" in leaks
        conn.close()
