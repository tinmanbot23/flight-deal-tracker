"""Tests for the post-storage filter-leak audit."""
from __future__ import annotations

import json

import pytest

import audit
import db
from conftest import make_row

FILTERS = {
    "max_price_usd": 1000,
    "max_stops": 1,
    "max_total_duration_hours": 14,
    "min_connection_minutes": 60,
    "basic_economy_patterns": ["BASIC", "LIGHT", "SAVER", "ECO BASIC"],
    "blocked_carriers": ["NK", "F9", "G4", "XP", "SY", "OG", "N0", "MX", "Y4", "VB"],
}
ORIGINS = {"GSO": "08:00", "CLT": "09:00", "RDU": "10:00"}


def two_seg(carrier, dep_apt, dep, hub, mid_arr, mid_dep, arr_apt, arr, opcarrier=None):
    return [
        {"carrier": carrier, "operating_carrier": opcarrier or carrier,
         "flight_number": f"{carrier}100", "depart_airport": dep_apt, "depart_time": dep,
         "arrive_airport": hub, "arrive_time": mid_arr, "duration_minutes": 90},
        {"carrier": carrier, "operating_carrier": opcarrier or carrier,
         "flight_number": f"{carrier}200", "depart_airport": hub, "depart_time": mid_dep,
         "arrive_airport": arr_apt, "arrive_time": arr, "duration_minutes": 120},
    ]


def clean_row(**overrides):
    out = two_seg("DL", "RDU", "2026-11-09T11:40:00", "ATL",
                  "2026-11-09T13:10:00", "2026-11-09T14:30:00", "SCL", "2026-11-09T19:40:00")
    inb = two_seg("DL", "SCL", "2026-11-17T08:05:00", "ATL",
                  "2026-11-17T15:10:00", "2026-11-17T16:40:00", "RDU", "2026-11-17T18:12:00")
    row = make_row(
        origin="RDU", outbound_json=json.dumps(out), inbound_json=json.dumps(inb),
        fare_brand_names=json.dumps(["MAIN CABIN"] * 4), price_usd=842.0,
    )
    row.update(overrides)
    return row


class TestAuditRow:
    def test_clean_offer_has_no_violations(self):
        assert audit.audit_row(clean_row(), FILTERS, ORIGINS) == []

    def test_overpriced_caught(self):
        v = audit.audit_row(clean_row(price_usd=1200), FILTERS, ORIGINS)
        assert any("price" in x for x in v)

    def test_short_connection_caught(self):
        out = two_seg("DL", "RDU", "2026-11-09T11:40:00", "ATL",
                      "2026-11-09T13:10:00", "2026-11-09T13:40:00", "SCL", "2026-11-09T19:40:00")
        v = audit.audit_row(clean_row(outbound_json=json.dumps(out)), FILTERS, ORIGINS)
        assert any("connection" in x for x in v)

    def test_long_direction_caught(self):
        out = two_seg("DL", "RDU", "2026-11-09T06:00:00", "ATL",
                      "2026-11-09T13:10:00", "2026-11-09T14:30:00", "SCL", "2026-11-09T21:00:00")
        # depart 06:00 also under RDU floor, but duration must be flagged too.
        v = audit.audit_row(clean_row(outbound_json=json.dumps(out)), FILTERS, ORIGINS)
        assert any("duration" in x for x in v)

    def test_blocked_operating_carrier_caught(self):
        out = two_seg("DL", "RDU", "2026-11-09T11:40:00", "ATL",
                      "2026-11-09T13:10:00", "2026-11-09T14:30:00", "SCL", "2026-11-09T19:40:00",
                      opcarrier="F9")
        v = audit.audit_row(clean_row(outbound_json=json.dumps(out)), FILTERS, ORIGINS)
        assert any("blocked carrier" in x for x in v)

    def test_basic_economy_caught(self):
        row = clean_row(fare_brand_names=json.dumps(["MAIN CABIN", "BASIC ECONOMY", "MAIN CABIN", "MAIN CABIN"]))
        v = audit.audit_row(row, FILTERS, ORIGINS)
        assert any("basic" in x for x in v)

    def test_missing_brand_caught(self):
        row = clean_row(fare_brand_names=json.dumps(["MAIN CABIN", None, "MAIN CABIN", "MAIN CABIN"]))
        assert any("basic" in x for x in audit.audit_row(row, FILTERS, ORIGINS))

    def test_departure_floor_caught(self):
        out = two_seg("DL", "RDU", "2026-11-09T09:30:00", "ATL",
                      "2026-11-09T11:00:00", "2026-11-09T12:30:00", "SCL", "2026-11-09T17:40:00")
        # 09:30 < RDU 10:00 floor.
        v = audit.audit_row(clean_row(outbound_json=json.dumps(out)), FILTERS, ORIGINS)
        assert any("floor" in x for x in v)


class TestAuditRun:
    def test_clean_run_reports_no_leaks(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        db.insert_offer(conn, clean_row(offer_hash="clean1"))
        conn.commit()
        config = {"filters": FILTERS, "origins": [{"code": "RDU", "earliest_departure": "10:00"}]}
        assert audit.audit_run(conn, config) == {}

    def test_leaky_row_is_flagged(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        db.insert_offer(conn, clean_row(offer_hash="bad1", price_usd=1500))
        conn.commit()
        config = {"filters": FILTERS, "origins": [{"code": "RDU", "earliest_departure": "10:00"}]}
        leaks = audit.audit_run(conn, config)
        assert "bad1" in leaks
