"""Tests for ranking / value scoring."""
from __future__ import annotations

import db
import rank
from conftest import make_offer_row

CONFIG = {
    "ranking": {"history_days": 30, "min_history_offers": 8},
    "filters": {"max_price_usd": 1000},
}


class TestValueScore:
    def test_uses_median_when_history_deep(self):
        history = [400] * 8
        assert rank.value_score(200, history, 1000, 8) == 0.5

    def test_falls_back_to_price_over_cap_when_thin(self):
        assert rank.value_score(500, [400, 400], 1000, 8) == 0.5


class TestTopDeals:
    def _seed(self, conn, rows):
        for r in rows:
            db.insert_offer(conn, make_offer_row(**r))
        conn.commit()

    def test_one_deal_per_destination(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        ts = "2026-07-09T08:00:00Z"
        self._seed(conn, [
            {"origin": "RDU", "dest": "DEN", "price_usd": 250, "offer_hash": "a", "run_timestamp": ts},
            {"origin": "GSO", "dest": "DEN", "price_usd": 200, "offer_hash": "b", "run_timestamp": ts},
            {"origin": "RDU", "dest": "LAS", "price_usd": 300, "offer_hash": "c", "run_timestamp": ts},
        ])
        top = rank.top_deals(conn, CONFIG, "domestic")
        conn.close()
        assert [d["dest"] for d in top] == ["DEN", "LAS"]     # no duplicate DEN
        den = next(d for d in top if d["dest"] == "DEN")
        assert den["price_usd"] == 200                        # cheapest DEN chosen

    def test_tiebreak_prefers_fewer_stops(self, tmp_path):
        conn = db.connect(str(tmp_path / "t.db"))
        ts = "2026-07-09T08:00:00Z"
        self._seed(conn, [
            {"origin": "RDU", "dest": "DEN", "price_usd": 250, "stops": 1, "offer_hash": "a", "run_timestamp": ts},
            {"origin": "GSO", "dest": "MCO", "price_usd": 250, "stops": 0, "offer_hash": "b", "run_timestamp": ts},
        ])
        top = rank.top_deals(conn, CONFIG, "domestic")
        conn.close()
        assert top[0]["stops"] == 0
