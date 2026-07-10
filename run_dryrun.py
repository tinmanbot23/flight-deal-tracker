"""Targeted dry run: GSO+CLT+RDU -> SCL and MCO, against Amadeus TEST.

Loads a .env (if present), runs the tracker with config.dryrun.yml into an
isolated database, then prints the rows written, the Top 3 per region, writes
docs/data/prices.json for the dashboard, and audits every stored offer for
filter leaks.

Usage:
    python run_dryrun.py               # live: needs AMADEUS_* creds (.env or env)
    python run_dryrun.py --mock        # offline: synthetic offers, no network

--mock injects deliberately bad offers (basic economy, a blocked carrier, an
overpriced fare, a too-short connection) alongside good ones to prove the
filters reject them and the audit finds zero leaks in what gets stored.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DRYRUN_DB = str(HERE / "dryrun.db")
DRYRUN_CONFIG = str(HERE / "config.dryrun.yml")

log = logging.getLogger("dryrun")


def load_dotenv(path: Path) -> bool:
    """Load KEY=VALUE lines from a .env file into os.environ. Returns found?"""
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return True


# --------------------------------------------------------------------------
# Mock Amadeus client (offline mode only)
# --------------------------------------------------------------------------
HUBS = {"GSO": "ATL", "CLT": "MIA", "RDU": "ATL"}
CARRIER_NAMES = {"DL": "DELTA AIR LINES", "AA": "AMERICAN AIRLINES", "NK": "SPIRIT AIRLINES"}


def _seg(seg_id, carrier, num, dep, dep_t, arr, arr_t, dur, operating=None):
    return {
        "id": str(seg_id), "carrierCode": carrier, "number": num,
        "aircraft": {"code": "321"}, "operating": {"carrierCode": operating or carrier},
        "duration": dur,
        "departure": {"iataCode": dep, "at": dep_t},
        "arrival": {"iataCode": arr, "at": arr_t},
        "numberOfStops": 0,
    }


def _offer(offer_id, price, brand, origin, dest, depart, ret, carrier="DL",
           operating=None, out_conn_minutes=90):
    """One round-trip offer in Amadeus response shape."""
    hub = HUBS[origin]
    d, r = depart, ret
    from datetime import datetime as _dt, timedelta as _td
    mid_arr_dt = _dt.fromisoformat(f"{d}T13:10:00")
    mid_arr = mid_arr_dt.isoformat()
    mid_dep = (mid_arr_dt + _td(minutes=out_conn_minutes)).isoformat()
    out = [
        _seg(f"{offer_id}a", carrier, "1103", origin, f"{d}T11:40:00", hub, mid_arr, "PT1H30M", operating),
        _seg(f"{offer_id}b", carrier, "147", hub, mid_dep, dest, f"{d}T20:40:00", "PT6H10M", operating),
    ]
    inb = [
        _seg(f"{offer_id}c", carrier, "146", dest, f"{r}T08:05:00", hub, f"{r}T15:10:00", "PT6H5M", operating),
        _seg(f"{offer_id}d", carrier, "2088", hub, f"{r}T16:40:00", origin, f"{r}T18:12:00", "PT1H32M", operating),
    ]
    fare = [{"segmentId": s["id"], "cabin": "ECONOMY",
             "brandedFare": brand.replace(" ", ""), "brandedFareLabel": brand}
            for s in out + inb]
    return {
        "type": "flight-offer", "id": str(offer_id),
        "itineraries": [
            {"duration": "PT9H0M", "segments": out},
            {"duration": "PT10H7M", "segments": inb},
        ],
        "price": {"currency": "USD", "total": f"{price:.2f}", "grandTotal": f"{price:.2f}"},
        "validatingAirlineCodes": [carrier],
        "travelerPricings": [{
            "travelerId": "1", "fareOption": "STANDARD", "travelerType": "ADULT",
            "price": {"currency": "USD", "total": f"{price:.2f}"},
            "fareDetailsBySegment": fare,
        }],
    }


class MockAmadeusClient:
    """Stand-in returning a fixed mix of good and deliberately bad offers."""

    def __init__(self, *args, **kwargs):
        self.calls_made = 0
        self.on_call = kwargs.get("on_call")

    def search_flight_offers(self, *, origin, destination, departure_date,
                             return_date, **kwargs):
        self.calls_made += 1
        if self.on_call:
            self.on_call()
        base = 780 if destination == "SCL" else 240
        d, r = departure_date, return_date
        data = [
            # Good main-cabin offers (should pass).
            _offer(1, base + 62, "MAIN CABIN", origin, destination, d, r, "DL"),
            _offer(2, base + 9, "MAIN CABIN", origin, destination, d, r, "AA"),
            # Basic economy (must be rejected).
            _offer(3, base - 40, "BASIC ECONOMY", origin, destination, d, r, "DL"),
            # Blocked carrier, operated by Spirit (must be rejected).
            _offer(4, base - 10, "MAIN CABIN", origin, destination, d, r, "AA", operating="NK"),
            # Over price cap (must be rejected).
            _offer(5, 1450, "MAIN CABIN", origin, destination, d, r, "DL"),
            # Too-short connection: 30 min (must be rejected).
            _offer(6, base + 5, "MAIN CABIN", origin, destination, d, r, "AA", out_conn_minutes=30),
        ]
        return {"data": data, "dictionaries": {"carriers": CARRIER_NAMES,
                                               "aircraft": {"321": "AIRBUS A321"}}}


# --------------------------------------------------------------------------
def print_rows(conn):
    rows = conn.execute(
        "SELECT origin, dest, dest_region, depart_date, return_date, price_usd, "
        "stops_outbound, stops_inbound, fare_brand_names FROM offers ORDER BY dest_region, price_usd"
    ).fetchall()
    print(f"\n=== Rows written to prices.db: {len(rows)} ===")
    print(f"{'ORIG':4} {'DEST':4} {'REGION':13} {'DEPART':10} {'RETURN':10} {'PRICE':>7}  STOPS  BRAND")
    for r in rows:
        import json as _j
        brand = next((b for b in _j.loads(r["fare_brand_names"]) if b), "?")
        print(f"{r['origin']:4} {r['dest']:4} {r['dest_region']:13} {r['depart_date']:10} "
              f"{r['return_date']:10} ${r['price_usd']:>6.0f}  {r['stops_outbound']}/{r['stops_inbound']}    {brand}")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Use synthetic offers, no network.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    os.chdir(HERE)
    os.environ["PRICES_DB"] = DRYRUN_DB
    if Path(DRYRUN_DB).exists():
        Path(DRYRUN_DB).unlink()

    found_env = load_dotenv(HERE / ".env")
    print(f".env loaded: {found_env}")

    import tracker
    if args.mock:
        tracker.AmadeusClient = MockAmadeusClient
        print("MODE: MOCK (synthetic offers, includes deliberately bad ones)")
    else:
        if not os.environ.get("AMADEUS_CLIENT_ID") or not os.environ.get("AMADEUS_CLIENT_SECRET"):
            print("\nERROR: AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET not set.")
            print("Add them to flight-deal-tracker/.env or the environment, or run with --mock.")
            return 2
        os.environ.setdefault("AMADEUS_ENV", "test")
        print(f"MODE: LIVE (Amadeus {os.environ.get('AMADEUS_ENV')})")

    # 1. Track.
    rc = tracker.run(config_path=DRYRUN_CONFIG, db_path=DRYRUN_DB)
    if rc != 0:
        return rc

    import db
    import rank
    import export
    import audit

    conn = db.connect(DRYRUN_DB)
    n_rows = print_rows(conn)

    # 2. Top 3.
    config = tracker.load_config(DRYRUN_CONFIG)
    tops = rank.rank_all(conn, config)
    from alerts import format_deal
    print("\n=== Top 3 ===")
    for region in ("domestic", "international"):
        print(f"-- {region} --")
        deals = tops[region]
        if not deals:
            print("   (none)")
        for i, d in enumerate(deals, 1):
            print(f"  {i}. {format_deal(d)}  [score {d['value_score']:.2f}]")

    # 3. Export for the dashboard.
    export.main()

    # 4. Audit: catch anything that slipped past a filter.
    leaks = audit.audit_run(conn, config)
    print("\n=== Filter-leak audit ===")
    if not leaks:
        print(f"  CLEAN: all {n_rows} stored offers satisfy every filter.")
    else:
        print(f"  LEAKS FOUND in {len(leaks)} offer(s):")
        for h, problems in leaks.items():
            print(f"    {h}: {'; '.join(problems)}")
    return 0 if not leaks else 1


if __name__ == "__main__":
    raise SystemExit(main())
