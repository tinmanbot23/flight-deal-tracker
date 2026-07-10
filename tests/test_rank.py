"""Ranking logic tests: value score, history fallback, tiebreaks, dest dedupe."""
from __future__ import annotations

import pytest

import db
import rank
from conftest import make_row

CONFIG = {
    "filters": {"max_price_usd": 1000},
    "ranking": {"history_days": 30, "min_history_offers": 3},
}

OLD_RUN = "2026-07-01T08:00:00Z"
CURRENT_RUN = "2026-07-09T08:00:00Z"


@pytest.fixture()
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


def seed(conn, **overrides):
    db.insert_offer(conn, make_row(**overrides))
    conn.commit()


class TestValueScore:
    def test_uses_median_with_enough_history(self):
        assert rank.value_score(300, [500, 500, 500], 1000, 3) == pytest.approx(0.6)

    def test_falls_back_to_absolute_price_when_history_thin(self):
        assert rank.value_score(400, [500], 1000, 3) == pytest.approx(0.4)


class TestTopDeals:
    def test_median_deal_beats_pricier_ratio(self, conn):
        # GSO-DEN history: median $500. Current $300 => score 0.6.
        for i in range(4):
            seed(conn, run_timestamp=OLD_RUN, origin="GSO", dest="DEN",
                 dest_region="domestic", price_usd=500, offer_hash=f"hist{i}")
        seed(conn, run_timestamp=CURRENT_RUN, origin="GSO", dest="DEN",
             dest_region="domestic", price_usd=300, offer_hash="cur-den")
        # GSO-LAS, no history, $700 => fallback score 0.7.
        seed(conn, run_timestamp=CURRENT_RUN, origin="GSO", dest="LAS",
             dest_region="domestic", price_usd=700, offer_hash="cur-las")

        top = rank.top_deals(conn, CONFIG, "domestic")
        assert [d["dest"] for d in top] == ["DEN", "LAS"]
        assert top[0]["value_score"] == pytest.approx(0.6)

    def test_history_excludes_current_run(self, conn):
        # Only current-run offers exist: history must be empty => fallback score.
        seed(conn, run_timestamp=CURRENT_RUN, origin="GSO", dest="DEN",
             dest_region="domestic", price_usd=400, offer_hash="a")
        seed(conn, run_timestamp=CURRENT_RUN, origin="GSO", dest="DEN",
             dest_region="domestic", price_usd=500, offer_hash="b")
        top = rank.top_deals(conn, CONFIG, "domestic")
        assert top[0]["value_score"] == pytest.approx(0.4)

    def test_never_two_deals_for_same_destination(self, conn):
        for i, (origin, price) in enumerate([("GSO", 300), ("RDU", 350), ("CLT", 400)]):
            seed(conn, run_timestamp=CURRENT_RUN, origin=origin, dest="DEN",
                 dest_region="domestic", price_usd=price, offer_hash=f"den{i}")
        seed(conn, run_timestamp=CURRENT_RUN, origin="GSO", dest="LAS",
             dest_region="domestic", price_usd=900, offer_hash="las")

        top = rank.top_deals(conn, CONFIG, "domestic")
        dests = [d["dest"] for d in top]
        assert len(dests) == len(set(dests)) == 2
        assert dests == ["DEN", "LAS"]

    def test_tiebreak_duration_then_stops(self, conn):
        seed(conn, run_timestamp=CURRENT_RUN, origin="GSO", dest="DEN",
             dest_region="domestic", price_usd=400, total_duration_minutes=600,
             offer_hash="slow")
        seed(conn, run_timestamp=CURRENT_RUN, origin="RDU", dest="MCO",
             dest_region="domestic", price_usd=400, total_duration_minutes=500,
             offer_hash="fast")
        top = rank.top_deals(conn, CONFIG, "domestic")
        assert top[0]["dest"] == "MCO"

        seed(conn, run_timestamp=CURRENT_RUN, origin="CLT", dest="TPA",
             dest_region="domestic", price_usd=400, total_duration_minutes=500,
             stops_outbound=0, stops_inbound=0, offer_hash="nonstop")
        top = rank.top_deals(conn, CONFIG, "domestic")
        assert top[0]["dest"] == "TPA"

    def test_regions_ranked_separately(self, conn):
        seed(conn, run_timestamp=CURRENT_RUN, origin="GSO", dest="DEN",
             dest_region="domestic", price_usd=200, offer_hash="dom")
        seed(conn, run_timestamp=CURRENT_RUN, origin="RDU", dest="SCL",
             dest_region="international", price_usd=842, offer_hash="intl")
        tops = rank.rank_all(conn, CONFIG)
        assert [d["dest"] for d in tops["domestic"]] == ["DEN"]
        assert [d["dest"] for d in tops["international"]] == ["SCL"]

    def test_empty_db(self, conn):
        assert rank.top_deals(conn, CONFIG, "domestic") == []
