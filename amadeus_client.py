"""Thin Amadeus Self-Service API client.

OAuth2 client-credentials auth (token cached for the lifetime of the client,
i.e. one run), a Flight Offers Search wrapper, retry with exponential backoff
on 429/5xx, a 10 requests/second ceiling, and a per-run call counter.
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

BASE_URLS = {
    "test": "https://test.api.amadeus.com",
    "production": "https://api.amadeus.com",
}
TOKEN_PATH = "/v1/security/oauth2/token"
FLIGHT_OFFERS_PATH = "/v2/shopping/flight-offers"

MIN_REQUEST_INTERVAL_SECONDS = 0.11  # keeps us under 10 requests/second
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 30


class AmadeusError(RuntimeError):
    """Raised when the Amadeus API returns an unrecoverable error."""


class AmadeusClient:
    """Minimal Amadeus API client for Flight Offers Search.

    Args:
        client_id / client_secret: default to AMADEUS_CLIENT_ID / _SECRET env vars.
        env: "test" or "production"; defaults to AMADEUS_ENV env var, then "test".
        session: injectable requests.Session (used by tests).
        on_call: optional callback invoked once per API call made (used to
            persist the monthly call count in the database as we go).
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        env: str | None = None,
        session: requests.Session | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.client_id = client_id or os.environ.get("AMADEUS_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("AMADEUS_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise AmadeusError(
                "Missing credentials: set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET"
            )
        env = (env or os.environ.get("AMADEUS_ENV") or "test").strip().lower()
        if env not in BASE_URLS:
            raise AmadeusError(f"AMADEUS_ENV must be 'test' or 'production', got {env!r}")
        self.env = env
        self.base_url = BASE_URLS[env]
        self.session = session or requests.Session()
        self.on_call = on_call
        self.calls_made = 0
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_request_at = 0.0

    def _get_token(self) -> str:
        """Return a valid OAuth2 bearer token, fetching one if needed."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = self.session.post(
            self.base_url + TOKEN_PATH,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise AmadeusError(f"Token request failed ({resp.status_code}): {resp.text[:300]}")
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 1799))
        log.debug("Obtained Amadeus token for env=%s", self.env)
        return self._token

    def _throttle(self) -> None:
        """Sleep as needed to stay under the requests/second ceiling."""
        wait = self._last_request_at + MIN_REQUEST_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make an authenticated request with retry/backoff on 429 and 5xx."""
        last_error = ""
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            resp = self.session.request(
                method,
                self.base_url + path,
                params=params,
                headers={"Authorization": f"Bearer {self._get_token()}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            self.calls_made += 1
            if self.on_call:
                self.on_call()
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                # Token expired mid-run: invalidate and retry immediately.
                self._token = None
                last_error = "401 unauthorized"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else BACKOFF_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.5)
                )
                last_error = f"{resp.status_code}: {resp.text[:200]}"
                log.warning(
                    "Amadeus %s (attempt %d/%d), backing off %.1fs",
                    resp.status_code, attempt + 1, MAX_RETRIES + 1, delay,
                )
                time.sleep(delay)
                continue
            raise AmadeusError(f"Amadeus API error {resp.status_code}: {resp.text[:500]}")
        raise AmadeusError(f"Retries exhausted; last error: {last_error}")

    def search_flight_offers(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str,
        adults: int = 1,
        currency: str = "USD",
        max_results: int = 20,
        travel_class: str = "ECONOMY",
    ) -> dict[str, Any]:
        """Round-trip Flight Offers Search (v2). Dates are YYYY-MM-DD.

        Returns the raw API response dict ({"data": [...], "dictionaries": {...}}).
        Branded fare names, where the airline provides them, appear in each
        offer's travelerPricings[].fareDetailsBySegment[].brandedFareLabel.
        """
        return self._request(
            "GET",
            FLIGHT_OFFERS_PATH,
            params={
                "originLocationCode": origin,
                "destinationLocationCode": destination,
                "departureDate": departure_date,
                "returnDate": return_date,
                "adults": adults,
                "currencyCode": currency,
                "max": max_results,
                "travelClass": travel_class,
            },
        )
