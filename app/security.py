"""Security helpers: Twilio webhook signature validation; dashboard auth lives in app.auth."""
from __future__ import annotations

import base64
import hashlib
import hmac

from .auth import require_owner, require_superadmin, require_user

# Every dashboard page and API needs a logged-in staff member (see app/auth.py).
require_admin = require_user

__all__ = ["is_valid_twilio_request", "require_admin", "require_owner", "require_superadmin", "require_user",
           "twilio_signature"]


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Compute X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted(key+value)))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid_twilio_request(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)
