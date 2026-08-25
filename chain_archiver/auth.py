"""OAuth session and the retrying HTTP client.

Deliberately a thin wrapper over httpx rather than the tastytrade SDK: this
touches four read-only endpoints, and a hand-rolled client has no version
churn and no chance of an order method existing on the object at all.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import httpx

from chain_archiver.config import API_VERSION, Settings

log = logging.getLogger(__name__)

#: Transient conditions worth a retry. Any other 4xx is a bug in our request,
#: not a blip, and retrying it just delays a real error (§6).
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

MAX_ATTEMPTS = 3

#: Conservative floor on request spacing. Thirty symbols twice a day is
#: nowhere near any published limit; this exists so that a future backfill
#: loop cannot accidentally hammer the API (§6).
MIN_REQUEST_INTERVAL = 0.12


class AuthError(RuntimeError):
    """Raised when the token exchange fails. Never retried, never swallowed."""


class ApiError(RuntimeError):
    """A non-transient API error for a single request."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _describe_error(response: httpx.Response) -> str:
    """Unpack tastytrade's {"error": {...}} envelope into something readable."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"

    error = payload.get("error")
    if not isinstance(error, dict):
        return f"HTTP {response.status_code}: {payload}"

    parts = []
    for item in error.get("errors") or [error]:
        if "code" in item and "message" in item:
            parts.append(f"{item['code']}: {item['message']}")
        elif "domain" in item and "reason" in item:
            parts.append(f"{item['domain']}: {item['reason']}")
        else:
            parts.append(str(item))
    return f"HTTP {response.status_code}: " + "; ".join(parts)


class TastytradeClient:
    """Authenticated, rate-limited, retrying GET-only client."""

    def __init__(self, settings: Settings, timeout: float = 30.0) -> None:
        self._settings = settings
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()
        self._last_request_at = 0.0

        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.send_api_version:
            headers["Accept-Version"] = API_VERSION

        self._client = httpx.Client(
            base_url=settings.api_url, headers=headers, timeout=timeout
        )

    def __enter__(self) -> TastytradeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- auth ------------------------------------------------------------

    def authenticate(self) -> None:
        """Force a token exchange. Called once up front so that bad
        credentials fail loudly before any symbol work starts (§6)."""
        self._refresh(force=True)

    def _refresh(self, force: bool = False) -> None:
        # 60s buffer so a token cannot expire mid-flight.
        if not force and time.time() < self._token_expires_at - 60:
            return
        with self._lock:
            if not force and time.time() < self._token_expires_at - 60:
                return
            request = self._client.build_request(
                "POST",
                "/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_secret": self._settings.client_secret,
                    "refresh_token": self._settings.refresh_token,
                },
            )
            # The stale bearer token must not ride along on the exchange.
            request.headers.pop("Authorization", None)
            try:
                response = self._client.send(request)
            except httpx.HTTPError as exc:
                raise AuthError(f"Token request failed: {exc}") from exc

            if response.status_code // 100 != 2:
                raise AuthError(f"Token request rejected. {_describe_error(response)}")

            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise AuthError(f"Token response had no access_token: {payload}")

            lifetime = int(payload.get("expires_in", 900))
            self._token = token
            self._token_expires_at = time.time() + lifetime
            self._client.headers["Authorization"] = f"Bearer {token}"
            log.debug("Refreshed access token, expires in %ss", lifetime)

    # -- requests --------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.time()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET `path` and return the unwrapped `data` object.

        Retries transient failures with exponential backoff plus jitter.
        """
        self._refresh()

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = ApiError(f"{path}: connection error: {exc}")
                if attempt == MAX_ATTEMPTS:
                    break
            else:
                if response.status_code // 100 == 2:
                    payload = response.json()
                    data = payload.get("data")
                    if data is None:
                        raise ApiError(f"{path}: response had no 'data': {payload}")
                    return data

                message = f"{path}: {_describe_error(response)}"
                if response.status_code not in RETRY_STATUS:
                    # A 401 here means the token went bad between refreshes;
                    # everything else in this branch is our bug to fix.
                    raise ApiError(message, response.status_code)
                last_error = ApiError(message, response.status_code)
                if attempt == MAX_ATTEMPTS:
                    break

            backoff = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "%s (attempt %d/%d), retrying in %.1fs",
                last_error,
                attempt,
                MAX_ATTEMPTS,
                backoff,
            )
            time.sleep(backoff)

        assert last_error is not None
        raise last_error
