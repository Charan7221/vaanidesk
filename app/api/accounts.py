"""Login, staff accounts, clinic setup and (platform admin) clinic management."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from ..auth import (
    CLINIC_COOKIE,
    ROLES,
    SESSION_COOKIE,
    SESSION_DAYS,
    SUPERADMIN_ID,
    audit,
    authenticate,
    hash_password,
    make_session_token,
    password_problem,
    require_owner,
    require_superadmin,
    require_user,
)
from ..business import Business
from ..config import ROOT_DIR
from ..db import DEFAULT_CLINIC, Appointment, AuditLog, Call, Clinic, OpdToken, User, as_utc, current_clinic
from ..phone import normalize_phone

router = APIRouter(prefix="/api", tags=["accounts"])
TEMPLATES = {"clinic": ROOT_DIR / "config" / "business.yaml", "hospital": ROOT_DIR / "config" / "hospital.yaml"}


def _cookie(response: Response, request: Request, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")


# ------------------------------------------------------------------ login
class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    state = request.app.state.container
    p = await authenticate(state, body.email.strip(), body.password)
    if p is None:
        raise HTTPException(401, "Wrong email or password")
    _cookie(response, request, SESSION_COOKIE, make_session_token(state.settings, p.id), SESSION_DAYS * 86400)
    request.state.principal = p
    if p.clinic_id:
        current_clinic.set(p.clinic_id)
    await audit(request, "login")
    return {"email": p.email, "role": p.role}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CLINIC_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(request: Request, p=Depends(require_user)) -> dict:
    state = request.app.state.container
    out = {"email": p.email, "name": p.name, "role": p.role, "clinic_id": state.clinic_id,
           "clinic_name": state.business.name, "pilot_mode": state.business.pilot_mode}
    if p.is_superadmin:
        out["clinics"] = [{"id": cid, "name": t.business.name} for cid, t in state.tenants.items()]
    return out


# ------------------------------------------------------------------ staff accounts (clinic owner)
class StaffIn(BaseModel):
    email: str = Field(min_length=3, max_length=160)
    name: str = Field(default="", max_length=120)
    role: str = "staff"
    password: str


class StaffUpdate(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = None


def _check_email(email: str) -> str:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(422, "Enter a valid email address")
    return email


async def create_user(state, clinic_id: str, email: str, name: str, role: str, password: str) -> User:
    if role not in ROLES:
        raise HTTPException(422, f"role must be one of {', '.join(ROLES)}")
    if problem := password_problem(password):
        raise HTTPException(422, problem)
    email = _check_email(email)
    async with state.db.session() as s:
        taken = (await s.execute(select(User).where(func.lower(User.email) == email)
                                 .execution_options(all_clinics=True))).scalars().first()
        if taken or email == state.settings.admin_user.lower():
            raise HTTPException(409, "That email already has an account")
        user = User(email=email, name=name.strip(), role=role, password_hash=hash_password(password),
                    clinic_id=clinic_id)
        s.add(user)
        await s.commit()
        return user


@router.get("/staff")
async def list_staff(request: Request, _=Depends(require_owner)) -> list[dict]:
    async with request.app.state.container.db.session() as s:
        users = (await s.execute(select(User).order_by(User.created_at))).scalars()
        return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.active,
                 "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None} for u in users]


@router.post("/staff")
async def add_staff(body: StaffIn, request: Request, _=Depends(require_owner)) -> dict:
    state = request.app.state.container
    user = await create_user(state, state.clinic_id, body.email, body.name, body.role, body.password)
    await audit(request, "staff.add", user.email, role=user.role)
    return {"id": user.id}


@router.post("/staff/{user_id}")
async def update_staff(user_id: str, body: StaffUpdate, request: Request, p=Depends(require_owner)) -> dict:
    async with request.app.state.container.db.session() as s:
        user = await s.get(User, user_id)
        if not user:
            raise HTTPException(404, "user not found")
        if user.id == p.id and (body.active is False or (body.role and body.role != user.role)):
            raise HTTPException(422, "You can't disable or demote your own account")
        changes = {}
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(422, f"role must be one of {', '.join(ROLES)}")
            user.role = changes["role"] = body.role
        if body.active is not None:
            user.active = changes["active"] = body.active
        if body.password:
            if problem := password_problem(body.password):
                raise HTTPException(422, problem)
            user.password_hash, changes["password"] = hash_password(body.password), "reset"
        await s.commit()
        email = user.email
    await audit(request, "staff.update", email, **changes)
    return {"ok": True}


@router.get("/audit")
async def audit_log(request: Request, limit: int = 100, _=Depends(require_owner)) -> list[dict]:
    tz = request.app.state.container.business.tz
    async with request.app.state.container.db.session() as s:
        rows = (await s.execute(select(AuditLog).order_by(AuditLog.at.desc()).limit(min(limit, 500)))).scalars()
        return [{"at": r.at.replace(tzinfo=r.at.tzinfo or UTC).astimezone(tz).strftime("%d %b %Y, %I:%M %p"),
                 "actor": r.actor, "action": r.action, "target": r.target, "detail": r.detail} for r in rows]


# ------------------------------------------------------------------ clinic setup (owner)
def _validate_config(state, clinic_id: str, config: dict) -> Business:
    try:
        biz = Business.model_validate(config)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise HTTPException(422, f"{'.'.join(map(str, first['loc']))}: {first['msg']}") from exc
    ids = [p.id for p in biz.providers]
    if not ids:
        raise HTTPException(422, "Add at least one doctor")
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Two doctors have the same id")
    for cid, t in state.tenants.items():
        clash = set(ids) & {p.id for p in t.business.providers}
        if cid != clinic_id and clash:
            raise HTTPException(422, f"Doctor id {sorted(clash)[0]!r} is used by another clinic; choose another id")
    depts = {d.id for d in biz.departments}
    for p in biz.providers:
        if p.department and p.department not in depts:
            raise HTTPException(422, f"{p.name}: unknown department {p.department!r}")
    for day, windows in biz.hours.items():
        for w in windows:
            if len(w) != 2 or w[0] >= w[1]:
                raise HTTPException(422, f"Opening hours on {day}: {w} is not a valid time range")
    return biz


@router.get("/setup")
async def get_setup(request: Request, _=Depends(require_owner)) -> dict:
    state = request.app.state.container
    async with state.db.session() as s:
        c = await s.get(Clinic, state.clinic_id)
    return {"clinic_id": state.clinic_id, "config": state.business.model_dump(mode="json"),
            "config_file": c.config_file if c else None, "phone_numbers": (c.phone_numbers if c else None) or []}


class SetupIn(BaseModel):
    config: dict


@router.put("/setup")
async def save_setup(body: SetupIn, request: Request, _=Depends(require_owner)) -> dict:
    state = request.app.state.container
    biz = _validate_config(state, state.clinic_id, body.config)
    await state.save_clinic_config(state.clinic_id, biz)
    await audit(request, "setup.save", biz.name)
    return {"saved": True}


# ------------------------------------------------------------------ platform admin: clinics
class ClinicIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,30}$")
    name: str = Field(min_length=2, max_length=160)
    template: str = "clinic"  # clinic | hospital
    owner_email: str
    owner_password: str
    phone_numbers: list[str] = []


class ClinicUpdate(BaseModel):
    phone_numbers: list[str] | None = None
    active: bool | None = None


def _numbers(raw: list[str]) -> list[str]:
    """Inbound clinic numbers: mobiles, landlines (+91 40 ...) or virtual numbers, in E.164."""
    out = []
    for n in raw:
        phone = normalize_phone(n)
        if not phone or not re.fullmatch(r"\+\d{10,15}", phone):
            raise HTTPException(422, f"{n}: not a valid phone number")
        out.append(phone)
    return out


@router.get("/admin/clinics")
async def list_clinics(request: Request, _=Depends(require_superadmin)) -> list[dict]:
    state = request.app.state.container
    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    async with state.db.session() as s:
        clinics = list((await s.execute(select(Clinic).order_by(Clinic.created_at))).scalars())
        for c in clinics:
            every = {"all_clinics": True}
            spans = (await s.execute(select(Call.started_at, Call.ended_at).where(
                Call.clinic_id == c.id, Call.started_at >= month).execution_options(**every))).all()
            seconds = sum((as_utc(e) - as_utc(st)).total_seconds() for st, e in spans if e)
            calls = (len(spans), seconds)
            booked = (await s.execute(select(func.count()).select_from(Appointment).where(
                Appointment.clinic_id == c.id, Appointment.created_at >= month).execution_options(**every))
                      ).scalar_one()
            tokens = (await s.execute(select(func.count()).select_from(OpdToken).where(
                OpdToken.clinic_id == c.id, OpdToken.created_at >= month).execution_options(**every))).scalar_one()
            staff = (await s.execute(select(func.count()).select_from(User).where(User.clinic_id == c.id)
                                     .execution_options(**every))).scalar_one()
            out.append({"id": c.id, "name": c.name, "active": c.active, "phone_numbers": c.phone_numbers or [],
                        "config_file": c.config_file, "staff": staff,
                        "this_month": {"calls": calls[0], "minutes": round((seconds or 0) / 60, 1),
                                       "appointments": booked, "opd_tokens": tokens}})
    return out


@router.post("/admin/clinics")
async def create_clinic(body: ClinicIn, request: Request, _=Depends(require_superadmin)) -> dict:
    state = request.app.state.container
    if body.id in state.tenants:
        raise HTTPException(409, "A clinic with that id exists")
    tpl = TEMPLATES.get(body.template)
    if tpl is None:
        raise HTTPException(422, f"template must be one of {', '.join(TEMPLATES)}")
    config = yaml.safe_load(Path(tpl).read_text(encoding="utf-8"))
    config["name"] = body.name
    # doctor ids must be unique across clinics: prefix the template's ids with the clinic id
    old_ids = {}
    for p in config.get("providers", []):
        old_ids[p["id"]] = p["id"] = f"{body.id}-{p['id']}"
    biz = _validate_config(state, body.id, config)
    numbers = _numbers(body.phone_numbers)
    async with state.db.session() as s:
        s.add(Clinic(id=body.id, name=body.name, config=biz.model_dump(mode="json"), phone_numbers=numbers))
        await s.commit()
    await create_user(state, body.id, body.owner_email, "Owner", "owner", body.owner_password)
    state.add_tenant(body.id, biz)
    await audit(request, "clinic.create", body.id, template=body.template)
    return {"id": body.id}


@router.post("/admin/clinics/{clinic_id}")
async def update_clinic(clinic_id: str, body: ClinicUpdate, request: Request, _=Depends(require_superadmin)) -> dict:
    state = request.app.state.container
    async with state.db.session() as s:
        c = await s.get(Clinic, clinic_id)
        if not c:
            raise HTTPException(404, "clinic not found")
        if body.phone_numbers is not None:
            c.phone_numbers = _numbers(body.phone_numbers)
        if body.active is not None:
            if clinic_id == DEFAULT_CLINIC and not body.active:
                raise HTTPException(422, "The first clinic can't be deactivated")
            c.active = body.active
        await s.commit()
        config, active = c.config, c.active
    if active and clinic_id not in state.tenants:
        state.add_tenant(clinic_id, Business.model_validate(config))
    if not active:
        state.tenants.pop(clinic_id, None)
    await audit(request, "clinic.update", clinic_id, **body.model_dump(exclude_none=True))
    return {"ok": True}


@router.post("/admin/switch/{clinic_id}")
async def switch_clinic(clinic_id: str, request: Request, response: Response, _=Depends(require_superadmin)) -> dict:
    if clinic_id not in request.app.state.container.tenants:
        raise HTTPException(404, "clinic not found")
    _cookie(response, request, CLINIC_COOKIE, clinic_id, SESSION_DAYS * 86400)
    return {"clinic_id": clinic_id}


__all__ = ["router", "SUPERADMIN_ID"]
