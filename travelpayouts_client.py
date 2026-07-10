"""Travelpayouts (Aviasales) Flight Data API client.

Replaces the decommissioned Amadeus Self-Service client. Uses the cached
"prices for the calendar" endpoint, which returns the cheapest round-trip
ticket for each departure date in a month — with airline, flight number, and
stop count — in a single call per route/month.

Auth: an account-level affiliate token (env TRAVELPAYOUTS_TOKEN), sent in the
X-Access-Token header. Sign up free at travelpayouts.com; the token lives in
your profile.

Note: the cached data does NOT include branded fares or per-segment routing,
so main-cabin/connection/duration filtering is not available on this source.
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.travelpayouts.com"
CALENDAR_PATH = "/v1/prices/calendar"

MIN_REQUEST_INTERVAL_SECONDS = 0.7  # be gentle; cached data has generous limits
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 30


class TravelpayoutsError(RuntimeError):
    """Raised when the Travelpayouts API returns an unrecoverable error."""


class TravelpayoutsClient:
    """Minimal client for the Travelpayouts flight-prices calendar endpoint.

    Args:
        token: defaults to the TRAVELPAYOUTS_TOKEN env var.
        currency: price currency (default "usd").
        session: injectable requests.Session (used by tests).
        on_call: optional callback invoked once per API call (used to persist
            the monthly call count as we go).
    """

    def __init__(
        self,
        token: str | None = None,
        currency: str = "usd",
        session: requests.Session | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.token = token or os.environ.get("TRAVELPAYOUTS_TOKEN", "")
        if not self.token:
            raise TravelpayoutsError(
                "Missing credentials: set TRAVELPAYOUTS_TOKEN (see your travelpayouts.com profile)"
            )
        self.currency = currency
        self.session = session or requests.Session()
        self.on_call = on_call
        self.calls_made = 0
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        """Sleep as needed to stay under the request-rate ceiling."""
        wait = self._last_request_at + MIN_REQUEST_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET with retry/backoff on 429 and 5xx; returns the parsed JSON."""
        last_error = ""
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            resp = self.session.get(
                BASE_URL + path,
                params=params,
                headers={"X-Access-Token": self.token},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            self.calls_made += 1
            if self.on_call:
                self.on_call()
            if resp.status_code == 200:
                payload = resp.json()
                if not payload.get("success", True):
                    raise TravelpayoutsError(f"API returned success=false: {payload}")
                return payload
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else BACKOFF_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.5)
                )
                last_error = f"{resp.status_code}: {resp.text[:200]}"
                log.warning(
                    "Travelpayouts %s (attempt %d/%d), backing off %.1fs",
                    resp.status_code, attempt + 1, MAX_RETRIES + 1, delay,
                )
                time.sleep(delay)
                continue
            raise TravelpayoutsError(
                f"Travelpayouts API error {resp.status_code}: {resp.text[:500]}"
            )
        raise TravelpayoutsError(f"Retries exhausted; last error: {last_error}")

    def prices_calendar(
        self,
        *,
        origin: str,
        destination: str,
        depart_month: str,
        length: int,
        currency: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Cheapest round-trip ticket per departure date for a month.

        Args:
            origin / destination: IATA codes.
            depart_month: "YYYY-MM" (the month to fetch departures for).
            length: length of stay in days (round-trip trip length).
            currency: overrides the client default.

        Returns the response's "data" mapping of departure date -> ticket dict.
        Each ticket has: origin, destination, price, transfers, airline,
        flight_number, departure_at, return_at, expires_at.
        """
        payload = self._request(
            CALENDAR_PATH,
            {
                "origin": origin,
                "destination": destination,
                "depart_date": depart_month,
                "calendar_type": "departure_date",
                "length": length,
                "currency": currency or self.currency,
            },
        )
        return payload.get("data", {}) or {}
