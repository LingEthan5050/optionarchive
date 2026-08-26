"""Dead-man's switch.

The failure mode that actually matters for a scheduled job is silent death:
it stops running and you notice in March. Nothing inside the process can warn
you about that, because the process is not running.

So the signal is inverted. On success the run pings a healthcheck URL, and an
external service alerts when a ping does not arrive on schedule. Silence is
the alarm.

Set HEALTHCHECK_URL in .env (healthchecks.io has a free tier; any service
with the same ping-or-alert shape works). Unset, this is a no-op - the job
runs unmonitored rather than refusing to run.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 10.0


def ping(url: str | None, *, suffix: str = "", body: str = "") -> None:
    """Ping the healthcheck endpoint. Never raises.

    A monitoring failure must never fail a capture that already succeeded -
    the snapshot on disk is the valuable thing.

    suffix: "" for success, "/fail" to signal failure explicitly, "/start"
    to mark the beginning of a run so duration can be tracked.
    """
    if not url:
        return
    target = url.rstrip("/") + suffix
    try:
        httpx.post(target, content=body.encode()[:10_000], timeout=TIMEOUT)
        log.debug("Healthcheck pinged: %s", target)
    except httpx.HTTPError as exc:
        # Worth a warning: if pings are failing, the dead-man's switch will
        # alert on a job that is actually fine, and you want to know why.
        log.warning("Healthcheck ping to %s failed: %s", target, exc)
