"""Patient messages: WhatsApp -> SMS fallback, email, the message log, and booking confirmations."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.background import drain
from app.main import create_app
from app.messaging import Message, Notifier, SmtpEmail, valid_email
from app.state import AppState
from app.tools.registry import ToolContext, ToolExecutor
from app.tools.twilio_client import TwilioClient

from .fakes import FakeLLM, FakeSTT, FakeTTS

AUTH = ("admin", "secret")


class FakeChannel:
    def __init__(self, ok=True, on=True):
        self.ok, self.on, self.sent = ok, on, []

    def enabled(self):
        return self.on

    async def send(self, to, msg):
        self.sent.append((to, msg.text))
        return (True, None) if self.ok else (False, "provider said no")


def notifier(settings, db, **channels):
    n = Notifier(settings, TwilioClient(settings), db)
    n.channels = {"whatsapp": FakeChannel(), "sms": FakeChannel(), "email": FakeChannel(), **channels}
    return n


def test_valid_email():
    assert valid_email(" Sai@Example.com ") == "sai@example.com"
    assert valid_email("not-an-email") is None
    assert valid_email(None) is None


async def test_whatsapp_falls_back_to_sms_and_logs(settings, db):
    n = notifier(settings, db, whatsapp=FakeChannel(ok=False))
    out = await n.send(kind="booked", msg=Message(text="Booked"), phone="+919346968532", email="a@b.co")
    assert out == {"sent": ["sms", "email"], "failed": ["whatsapp"]}
    log = {(m.channel, m.status) for m in await db.messages()}
    assert log == {("whatsapp", "failed"), ("sms", "sent"), ("email", "sent")}


async def test_nothing_configured_is_logged_not_raised(settings, db):
    n = notifier(settings, db, whatsapp=FakeChannel(on=False), sms=FakeChannel(on=False),
                 email=FakeChannel(on=False))
    out = await n.send(kind="booked", msg=Message(text="Hi"), phone="+919346968532", email="a@b.co")
    assert out == {"sent": [], "failed": []}
    assert {m.status for m in await db.messages()} == {"not_configured"}


async def test_clinic_settings_are_respected(settings, db, business):
    n = notifier(settings, db)
    business.notifications.phone_channels = ["sms"]
    business.notifications.email = False
    out = await n.notify_patient(business, kind="booked", msg=Message(text="x"), phone="+919346968532", email="a@b.co")
    assert out["sent"] == ["sms"] and not n.channels["whatsapp"].sent and not n.channels["email"].sent
    business.notifications.confirmations = False
    off = await n.notify_patient(business, kind="booked", msg=Message(text="x"), phone="+919346968532", email=None)
    assert off["sent"] == []
    # reminders still go out when confirmations are off
    rem = await n.notify_patient(business, kind="reminder", msg=Message(text="x"), phone="+919346968532", email=None)
    assert rem["sent"] == ["sms"]


def test_email_is_built_with_text_and_html(settings):
    settings.smtp_host, settings.smtp_from = "smtp.example.com", "clinic@example.com"
    ch = SmtpEmail(settings)
    assert ch.enabled()
    em = ch._build("p@example.com", Message(text="Your appointment", subject="Booked", title="Booked",
                                            lines=[("Doctor", "Dr. Rao")]))
    assert em["To"] == "p@example.com" and em["Subject"] == "Booked"
    kinds = [part.get_content_type() for part in em.walk()]
    assert "text/plain" in kinds and "text/html" in kinds


async def test_booking_sends_confirmation_with_email(settings, db, business, calendar, monday):
    n = notifier(settings, db)
    ex = ToolExecutor(business, calendar, TwilioClient(settings), notifier=n)
    ctx = ToolContext(call_id="c1", channel="browser", caller_phone="+919346968532")
    base = {"patient_name": "Sai Charan", "date": monday.isoformat(), "time": "17:20", "provider_id": "dr-rao"}
    bad = await ex.run(ctx, "book_appointment", {**base, "email": "nope"})
    assert bad["booked"] is False and "email" in bad["reason"]
    r = await ex.run(ctx, "book_appointment", {**base, "email": "sai@example.com"})
    assert r["booked"] is True and r["confirmation_channels"] == ["whatsapp", "email"]
    await drain()
    assert n.channels["whatsapp"].sent[0][0] == "+919346968532"
    assert n.channels["email"].sent[0][0] == "sai@example.com"
    assert (await db.get_appointment(r["appointment_id"])).email == "sai@example.com"


def test_dashboard_message_api(settings):
    state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM(), tts=FakeTTS())
    state.notifier.channels = {"whatsapp": FakeChannel(), "sms": FakeChannel(on=False), "email": FakeChannel(on=False)}
    with TestClient(create_app(settings, state)) as c:
        assert c.post("/api/messages", auth=AUTH, json={"text": "hi"}).status_code == 422
        assert c.post("/api/messages", auth=AUTH, json={"phone": "123", "text": "hi"}).status_code == 422
        assert c.post("/api/messages", auth=AUTH, json={"phone": "+919346968532", "text": " "}).status_code == 422
        r = c.post("/api/messages", auth=AUTH, json={"phone": "+91 93469 68532", "text": "Doctor is late"})
        assert r.status_code == 200 and r.json()["sent"] == ["whatsapp"]
        d = c.get("/api/messages", auth=AUTH).json()
        assert d["channels"] == {"whatsapp": True, "sms": False, "email": False}
        assert d["messages"][0]["kind"] == "manual" and d["messages"][0]["status"] == "sent"
        assert c.get("/api/messages").status_code == 401
