"""Security helpers: Twilio webhook signature validation; dashboard auth lives in app.auth."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from .auth import require_owner, require_superadmin, require_user

# Every dashboard page and API needs a logged-in staff member (see app/auth.py).
require_admin = require_user

__all__ = ["RateLimiter", "client_ip", "is_valid_twilio_request", "limiter", "require_admin", "require_owner",
           "require_superadmin", "require_user", "twilio_signature"]


class RateLimiter:
    """In-memory sliding-window counter (per server process): slows down password guessing, sign-up spam,
    chat abuse (AI cost) and one-time-code flooding."""

    def __init__(self):
        self.hits: dict[str, list[float]] = {}

    def _recent(self, key: str, window_s: int) -> list[float]:
        now = time.monotonic()
        self.hits[key] = [t for t in self.hits.get(key, []) if now - t < window_s]
        return self.hits[key]

    def allow(self, key: str, limit: int, window_s: int, count: bool = True) -> bool:
        recent = self._recent(key, window_s)
        if len(recent) >= limit:
            return False
        if count:
            recent.append(time.monotonic())
        return True

    def hit(self, key: str) -> None:
        self.hits.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)


limiter = RateLimiter()


def client_ip(headers, client) -> str:
    """The visitor's address. Behind one reverse proxy (Render, nginx) the proxy appends the real address as
    the LAST X-Forwarded-For entry; earlier entries are whatever the client sent and can be faked."""
    fwd = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    return fwd[-1] if fwd else (client.host if client else "?")


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Compute X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted(key+value)))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid_twilio_request(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)
