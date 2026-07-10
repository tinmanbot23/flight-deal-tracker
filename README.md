# Flight Deal Tracker

Serverless flight-deal tracker for GSO / CLT / RDU. Runs three times a day on
GitHub Actions cron, queries the Amadeus Self-Service **Flight Offers Search**
API (v2, with branded fares), filters out basic economy and ultra-low-cost
carriers, stores price history in SQLite, sends Pushover alerts, and publishes
a GitHub Pages dashboard.

The latest report lives in [report.md](report.md); the interactive dashboard
is served from `docs/` via GitHub Pages.

## How it stays under the API budget

Naively checking everything would cost 3 origins × 30 destinations × 2 windows
× 3 dates = 540 calls per run (~48,600/month) — far over the 2,500/month
budget. Instead, each run covers a **deterministic rotating subset**:

- `rotation.domestic_per_run: 2` + `rotation.international_per_run: 2`
  destinations per run (rotation index persisted in `prices.db`, so every
  destination is revisited about every 2.5 days),
- `rotation.windows_per_run: 1` date window,
- `rotation.dates_per_window: 2` evenly spaced departure dates.

That's 3 × 4 × 1 × 2 = **24 calls/run ≈ 2,160/month** at 3 runs/day. All four
knobs are in `config.yml`. A hard guard also tracks the monthly call count in
the database and skips a run (with a logged warning) if it would exceed
`api_budget.monthly_max_calls`.

## Filters (all configurable in `config.yml`)

- Max price $1,000 · max 1 stop · max 14h **per direction** · min 60-minute
  connections.
- **Main cabin or better only**: any segment whose branded fare matches
  BASIC / LIGHT / SAVER / ECO BASIC — or has no identifiable fare brand — is
  rejected. When in doubt, reject: every surfaced fare should include seat
  selection.
- **Blocked carriers** (marketing *or* operating): Spirit, Frontier,
  Allegiant, Avelo, Sun Country, Play, Norse, Breeze, Volaris, VivaAerobus.
- Per-origin earliest departure time, applied to the outbound first segment
  only (GSO 8:00a, CLT 9:00a, RDU 10:00a).

## Setup

1. **Create the repo** and push this project to the `main` branch.
2. **Amadeus credentials**: create a Self-Service app at
   [developers.amadeus.com](https://developers.amadeus.com), then add repo
   secrets (Settings → Secrets and variables → Actions):
   - `AMADEUS_CLIENT_ID`
   - `AMADEUS_CLIENT_SECRET`
   - Optionally set a repo *variable* `AMADEUS_ENV=production` once you have
     production keys. The default `test` environment returns limited, cached
     data — fine for wiring things up, not for real prices.
3. **Pushover (optional)**: add secrets `PUSHOVER_TOKEN` and `PUSHOVER_USER`.
   If unset, alerts are logged in the Actions output instead of sent.
4. **Enable Actions write access**: Settings → Actions → General → Workflow
   permissions → **Read and write permissions** (the workflow commits
   `prices.db`, `report.md`, and `docs/` back to `main`).
5. **Enable GitHub Pages**: Settings → Pages → Deploy from a branch →
   `main` / `docs`. The dashboard appears at
   `https://<user>.github.io/<repo>/`.
6. Run the workflow once by hand: Actions → *Track flights* → Run workflow.

## Schedule and daylight saving time

The cron `0 8,15,22 * * *` is UTC, i.e. **4am / 11am / 6pm EDT**. GitHub cron
has no time-zone support, so during standard time (early Nov → mid-Mar) the
runs shift to 3am / 10am / 5pm EST. If you care, edit the workflow to
`0 9,16,23 * * *` for the winter, or leave it — the tracker doesn't mind when
it runs.

## Local development

```bash
pip install -r requirements.txt
pytest                      # unit tests, no live API calls

# a real run (writes prices.db, then the derived artifacts):
export AMADEUS_CLIENT_ID=... AMADEUS_CLIENT_SECRET=...
python tracker.py && python rank.py && python alerts.py && python export.py && python report.py

# view the dashboard locally:
python -m http.server -d docs 8000   # -> http://localhost:8000
```

## Tuning filters with `analyze.py`

Every offer a filter rejects is recorded in the `rejections` table (reason,
fare brands, carriers). After a week or so of data, review it:

```bash
python analyze.py --days 7        # reads prices.db, makes no API calls
```

It reports three things:

1. **Per-filter rejection counts** — how many offers each filter dropped
   (first-failing-filter attribution). A filter rejecting almost everything
   may be too strict.
2. **Drop candidates** — routes that returned fares but never a qualifying
   one, plus routes with no data in the window. Candidates to remove from
   `config.yml`.
3. **Basic-economy review** — the distinct branded fares rejected as basic
   economy, ranked by frequency, with the trigger word each matched. If a
   legitimate main-cabin fare is being caught (e.g. an airline's
   "Economy Light Plus"), add its exact name to `filters.fare_brand_whitelist`
   in `config.yml` — an exact-match exemption that doesn't weaken the pattern
   for anything else. (A missing/blank brand is always rejected regardless.)

## Architecture

| File | Role |
|------|------|
| `config.yml` | Origins, destinations + date windows, filters, rotation, budget |
| `amadeus_client.py` | OAuth2, Flight Offers Search, retry/backoff, 10 req/s cap, call counter |
| `filters.py` | Pure, unit-testable filter functions + offer dedupe hash |
| `db.py` | SQLite: offers, rejections, monthly API-call budget, alert dedupe, run counter |
| `tracker.py` | One run: rotate destinations, sample dates, fetch → filter → store (passes + rejections) |
| `analyze.py` | Read-only: per-filter rejection rates, drop candidates, basic-economy whitelist review |
| `rank.py` | Top-3 per region by price vs trailing 30-day route median |
| `alerts.py` | Pushover top-3 summary + deduped below-threshold alerts |
| `export.py` | `docs/data/prices.json` for the dashboard (last 30 days) |
| `report.py` | `report.md` (top 3s + per-route cheapest with 7-day trend) |
| `docs/index.html` | Single-file Chart.js dashboard (top deals, route explorer, expandable flight details) |

Every stored offer keeps full segment detail (airline, flight numbers, exact
times, aircraft, layovers, fare brand), so anything surfaced can be found and
booked directly on the airline's site.
