"""Staff logins, roles and clinic selection.

* Each clinic's staff log in with their own email and password (``users`` table). Roles: ``owner`` (setup,
  staff accounts, everything) and ``staff`` (reception: dashboard, OPD, calendar). ``superadmin`` runs the
  platform and can open any clinic.
* ADMIN_USER / ADMIN_PASSWORD from .env is the platform super admin (also accepted as HTTP Basic auth, for
  scripts and tests).
* Login sets a signed, HttpOnly session cookie. Every authenticated request runs "inside" the user's clinic
  (``current_clinic``), so all queries are limited to that clinic's data.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy import func, select

from .db import DEFAULT_CLINIC, AuditLog, User, current_clinic, utcnow

SESSION_COOKIE = "vd_session"
CLINIC_COOKIE = "vd_clinic"  # super admin: which clinic they're looking at
SESSION_DAYS = 7
SUPERADMIN_ID = "__admin__"
ROLES = ("owner", "staff")


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        salt, digest = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    test = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=len(digest))
    return hmac.compare_digest(test, digest)


def password_problem(password: str) -> str | None:
    if len(password or "") < 10:
        return "Use at least 10 characters."
    if password.lower() in {"password123", "change-me", "1234567890", "qwertyuiop"}:
        return "That password is too common."
    return None


# ------------------------------------------------------------------ session cookie
def _secret(settings) -> bytes:
    key = settings.secret_key or ("vaanidesk-session:" + settings.admin_password)
    return hashlib.sha256(key.encode()).digest()


def make_session_token(settings, user_id: str, days: int = SESSION_DAYS) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": user_id, "exp": int(time.time()) + days * 86400}).encode())
    sig = hmac.new(_secret(settings), payload, hashlib.sha256).hexdigest()
    return payload.decode() + "." + sig


def read_session_token(settings, token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    good = hmac.new(_secret(settings), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    return data.get("u") if data.get("exp", 0) > time.time() else None


# ------------------------------------------------------------------ who is calling
@dataclass
class Principal:
    id: str
    email: str
    name: str
    role: str  # owner | staff | superadmin
    clinic_id: str | None  # None = super admin (any clinic)

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    @property
    def can_manage(self) -> bool:
        return self.role in ("owner", "superadmin")


def _superadmin(settings) -> Principal:
    return Principal(SUPERADMIN_ID, settings.admin_user, "Platform admin", "superadmin", None)


async def _user_by(db, **where) -> User | None:
    async with db.session() as s:
        q = select(User).where(User.active.is_(True)).execution_options(all_clinics=True)
        if "email" in where:
            q = q.where(func.lower(User.email) == where["email"].lower())
        if "id" in where:
            q = q.where(User.id == where["id"])
        return (await s.execute(q)).scalars().first()


def _principal(u: User) -> Principal:
    return Principal(u.id, u.email, u.name, u.role, u.clinic_id)


async def authenticate(state, email: str, password: str) -> Principal | None:
    s = state.settings
    if secrets.compare_digest(email.encode(), s.admin_user.encode()) and \
            secrets.compare_digest(password.encode(), s.admin_password.encode()):
        return _superadmin(s)
    user = await _user_by(state.db, email=email)
    if user and verify_password(password, user.password_hash):
        async with state.db.session() as sess:
            u = await sess.get(User, user.id, execution_options={"all_clinics": True})
            u.last_login_at = utcnow()
            await sess.commit()
        return _principal(user)
    return None


async def principal_from_request(request: Request) -> Principal | None:
    state = request.app.state.container
    uid = read_session_token(state.settings, request.cookies.get(SESSION_COOKIE))
    if uid == SUPERADMIN_ID:
        return _superadmin(state.settings)
    if uid:
        user = await _user_by(state.db, id=uid)
        return _principal(user) if user else None
    scheme, param = get_authorization_scheme_param(request.headers.get("Authorization"))
    if scheme.lower() == "basic" and param:
        try:
            email, _, password = base64.b64decode(param).decode().partition(":")
        except ValueError:
            return None
        return await authenticate(state, email, password)
    return None


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith("/api") and "text/html" in request.headers.get("accept", "")


async def require_user(request: Request) -> Principal:
    """Any logged-in staff member. Also selects the clinic for the rest of the request."""
    p = await principal_from_request(request)
    if p is None:
        if _wants_html(request):
            raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": f"/login?next={request.url.path}"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please log in")
    state = request.app.state.container
    clinic = p.clinic_id
    if p.is_superadmin:
        clinic = request.query_params.get("clinic") or request.cookies.get(CLINIC_COOKIE) or DEFAULT_CLINIC
    if clinic not in state.tenants:
        if p.is_superadmin:
            clinic = DEFAULT_CLINIC
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your clinic is not active")
    current_clinic.set(clinic)
    request.state.principal = p
    return p


async def require_owner(request: Request) -> Principal:
    p = await require_user(request)
    if not p.can_manage:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the clinic owner can do this")
    return p


async def require_superadmin(request: Request) -> Principal:
    p = await require_user(request)
    if not p.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin only")
    return p


async def audit(request: Request, action: str, target: str | None = None, **detail) -> None:
    """Record who did what (shown to clinic owners)."""
    p: Principal | None = getattr(request.state, "principal", None)
    async with request.app.state.container.db.session() as s:
        s.add(AuditLog(actor=p.email if p else "system", action=action, target=target, detail=detail or None))
        await s.commit()
