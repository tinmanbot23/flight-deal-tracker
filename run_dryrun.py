"""Targeted dry run: GSO+CLT+RDU -> SCL and MCO, against Travelpayouts.

Loads a .env (if present), runs the tracker with config.dryrun.yml into an
isolated database, then prints the rows written, the Top 3 per region, writes
docs/data/prices.json for the dashboard, and audits stored offers.

Usage:
    python run_dryrun.py               # live: needs TRAVELPAYOUTS_TOKEN
    python run_dryrun.py --mock        # offline: synthetic calendar, no network

--mock injects, across the month, tickets that violate each filter (blocked
carrier, overpriced, too many stops, too-early departure) alongside good ones
to prove the filters reject them and the audit finds zero leaks.
"""
from __future__ import annotations

import argparse
import calendar as _calendar
import logging
import os
import sys
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
# Mock Travelpayouts client (offline mode only)
# --------------------------------------------------------------------------
def _ticket(origin, dest, depart_date, length, price, airline, flight_number,
            transfers, dep_hhmm="11:40"):
    import datetime as _dt
    d = _dt.date.fromisoformat(depart_date)
    ret = (d + _dt.timedelta(days=length)).isoformat()
    return {
        "origin": origin, "destination": dest,
        "price": price, "transfers": transfers,
        "airline": airline, "flight_number": flight_number,
        "departure_at": f"{depart_date}T{dep_hhmm}:00Z",
        "return_at": f"{ret}T16:40:00Z",
        "expires_at": f"{depart_date}T00:00:00Z",
    }


class MockTravelpayoutsClient:
    """Stand-in returning a calendar with a mix of good and bad tickets."""

    def __init__(self, *args, on_call=None, **kwargs):
        self.calls_made = 0
        self.on_call = on_call

    def prices_calendar(self, *, origin, destination, depart_month, length, currency=None):
        self.calls_made += 1
        if self.on_call:
            self.on_call()
        base = 780 if destination == "SCL" else 240
        year, month = (int(x) for x in depart_month.split("-"))
        days = _calendar.monthrange(year, month)[1]
        data = {}
        for day in range(1, days + 1):
            depart = f"{year:04d}-{month:02d}-{day:02d}"
            r = day % 7
            if r == 0:      # blocked carrier (Spirit)
                t = _ticket(origin, destination, depart, length, base + 20, "NK", 100 + day, 1)
            elif r == 1:    # over price cap
                t = _ticket(origin, destination, depart, length, 1450, "DL", 100 + day, 1)
            elif r == 2:    # too many stops
                t = _ticket(origin, destination, depart, length, base + 15, "AA", 100 + day, 2)
            elif r == 3:    # departs too early (07:00 < every origin floor)
                t = _ticket(origin, destination, depart, length, base + 5, "DL", 100 + day, 1, "07:00")
            else:           # good
                t = _ticket(origin, destination, depart, length, base + day, "DL", 100 + day, day % 2)
            data[depart] = t
        return data


# --------------------------------------------------------------------------
def print_rows(conn):
    rows = conn.execute(
        "SELECT origin, dest, dest_region, depart_date, return_date, price_usd, "
        "airline, flight_number, stops FROM offers ORDER BY dest_region, price_usd LIMIT 40"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    print(f"\n=== Rows written to prices.db: {total} (showing up to 40) ===")
    print(f"{'ORIG':4} {'DEST':4} {'REGION':13} {'DEPART':10} {'RETURN':10} {'PRICE':>7}  AIR  FLT   STOPS")
    for r in rows:
        print(f"{r['origin']:4} {r['dest']:4} {r['dest_region']:13} {r['depart_date']:10} "
              f"{r['return_date']:10} ${r['price_usd']:>6.0f}  {str(r['airline']):3}  "
              f"{str(r['flight_number']):4}  {r['stops']}")
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Use synthetic data, no network.")
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
        tracker.TravelpayoutsClient = MockTravelpayoutsClient
        print("MODE: MOCK (synthetic calendar, includes deliberately bad tickets)")
    else:
        if not os.environ.get("TRAVELPAYOUTS_TOKEN"):
            print("\nERROR: TRAVELPAYOUTS_TOKEN not set.")
            print("Add it to flight-deal-tracker/.env or the environment, or run with --mock.")
            return 2
        print("MODE: LIVE (Travelpayouts)")

    rc = tracker.run(config_path=DRYRUN_CONFIG, db_path=DRYRUN_DB)
    if rc != 0:
        return rc

    import db
    import rank
    import export
    import audit

    conn = db.connect(DRYRUN_DB)
    print_rows(conn)

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

    export.main()

    leaks = audit.audit_run(conn, config)
    print("\n=== Filter-leak audit ===")
    if not leaks:
        total = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        print(f"  CLEAN: all {total} stored offers satisfy every filter.")
    else:
        print(f"  LEAKS FOUND in {len(leaks)} offer(s):")
        for h, problems in leaks.items():
            print(f"    {h}: {problems}")
    conn.close()
    return 0 if not leaks else 1


if __name__ == "__main__":
    raise SystemExit(main())
