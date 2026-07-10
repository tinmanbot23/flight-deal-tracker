# Flight Deal Tracker

Serverless flight-deal tracker for GSO / CLT / RDU. Runs three times a day on
GitHub Actions cron, queries the **Travelpayouts (Aviasales) flight-prices
calendar** API, filters out ultra-low-cost carriers and anything over budget,
stores price history in SQLite, sends Pushover alerts, and publishes a GitHub
Pages dashboard.

The latest report lives in [report.md](report.md); the interactive dashboard
is served from `docs/` via GitHub Pages.

> **Data source note.** This project originally used the Amadeus Self-Service
> API, which was decommissioned in July 2026. It now uses Travelpayouts'
> cached price data. That data is cheaper and free, but has **no branded fares
> or per-segment routing**, so the old main-cabin, minimum-connection, and
> per-direction duration filters are not possible here and have been removed.
> Prices are cached lowest fares — always confirm on the airline's own site
> before booking.

## How it works

The `/v1/prices/calendar` endpoint returns the cheapest round-trip ticket for
**every departure date in a month** in a single call, with airline, flight
number, and stop count. So one API call covers a whole month of a route.

Each run covers a **deterministic rotating subset** of destinations to keep
call volume and noise down:

- `rotation.domestic_per_run: 2` + `rotation.international_per_run: 2`
  destinations per run (rotation index persisted in `prices.db`, so every
  destination is revisited about every 2.5 days),
- `rotation.windows_per_run: 1` date window,
- one call per calendar month spanning that window.

A hard guard tracks the monthly call count in the database and skips a run
(with a logged warning) if it would exceed `api_budget.monthly_max_calls`.

## Filters (all configurable in `config.yml`)

- **Max price** $1,000 · **max 1 stop**.
- **Blocked carriers**: Spirit, Frontier, Allegiant, Avelo, Sun Country, Play,
  Norse, Breeze, Volaris, VivaAerobus. Cached data exposes only the validating
  airline, so a blocked *operating* carrier on a codeshare cannot be caught.
- **Per-origin earliest departure time** (GSO 8:00a, CLT 9:00a, RDU 10:00a),
  applied to the outbound departure.

## Setup

1. **Create the repo** and push this project to the `main` branch.
2. **Travelpayouts token**: sign up free at
   [travelpayouts.com](https://www.travelpayouts.com/), then copy your API
   token from your profile. Add it as a repo secret (Settings → Secrets and
   variables → Actions):
   - `TRAVELPAYOUTS_TOKEN`
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
export TRAVELPAYOUTS_TOKEN=...
python tracker.py && python rank.py && python alerts.py && python export.py && python report.py

# a fast targeted dry run (GSO/CLT/RDU -> SCL & MCO); --mock needs no token:
python run_dryrun.py --mock

# view the dashboard locally:
python -m http.server -d docs 8000   # -> http://localhost:8000
```

## Tuning filters with `analyze.py`

Every ticket a filter rejects is recorded in the `rejections` table (reason,
airline, stops). After a week or so of data, review it:

```bash
python analyze.py --days 7        # reads prices.db, makes no API calls
```

It reports:

1. **Per-filter rejection counts** — how many tickets each filter dropped
   (first-failing-filter attribution). A filter rejecting almost everything
   may be too strict.
2. **Drop candidates** — routes that returned fares but never a qualifying
   one, plus routes with no data in the window. Candidates to remove from
   `config.yml`.

## Architecture

| File | Role |
|------|------|
| `config.yml` | Origins, destinations + date windows, filters, rotation, budget |
| `travelpayouts_client.py` | Prices-calendar client: token auth, retry/backoff, rate limit, call counter |
| `filters.py` | Pure, unit-testable filter functions + offer dedupe hash |
| `db.py` | SQLite: offers, rejections, monthly API-call budget, alert dedupe, run counter |
| `tracker.py` | One run: rotate destinations, fetch calendars, filter → store (passes + rejections) |
| `analyze.py` | Read-only: per-filter rejection rates, drop candidates |
| `audit.py` | Re-validates stored offers against the filters (defense in depth) |
| `rank.py` | Top-3 per region by price vs trailing 30-day route median |
| `alerts.py` | Pushover top-3 summary + deduped below-threshold alerts |
| `export.py` | `docs/data/prices.json` for the dashboard (last 30 days) |
| `report.py` | `report.md` (top 3s + per-route cheapest with 7-day trend) |
| `docs/index.html` | Single-file Chart.js dashboard (top deals, route explorer, expandable detail) |

Each stored offer keeps price, airline, flight number, stop count, and exact
departure/return times, so a surfaced deal can be found and booked on the
airline's site.
