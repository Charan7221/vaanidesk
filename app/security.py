"""Security helpers: Twilio webhook signature validation and dashboard Basic auth."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Compute X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted(key+value)))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def is_valid_twilio_request(auth_token: str, url: str, params: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), signature)


_basic = HTTPBasic()


def require_admin(request: Request, creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    settings = request.app.state.container.settings
    ok_user = secrets.compare_digest(creds.username.encode(), settings.admin_user.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), settings.admin_password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials",
                            headers={"WWW-Authenticate": "Basic"})
    return creds.username
