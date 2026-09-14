"""Vendor and job errors that must not collapse into a miss."""

from __future__ import annotations


class VendorCallError(Exception):
    """HTTP throttle, timeout, or 5xx after retries. Not a lookup miss."""

    def __init__(self, tier: str, kind: str, message: str = "") -> None:
        self.tier = tier
        self.kind = kind
        super().__init__(message or f"{tier} {kind}")


class JobCancelled(Exception):
    """Caller asked the running job to stop."""
