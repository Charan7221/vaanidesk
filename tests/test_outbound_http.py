"""Real request/response handling for outbound services (Twilio, Meta WhatsApp, SMTP, Google Calendar,
Sarvam REST TTS), with the network replaced by httpx.MockTransport so nothing leaves the machine."""
from __future__ import annotations

import base64
import io
import json
import smtplib
import wave
from types import SimpleNamespace

import httpx
import pytest

from app.messaging import Message, MetaWhatsApp, Notifier, SmtpEmail
from app.providers import tts_sarvam
from app.tools.calendar import GoogleCalendarSync, LocalCalendar
from app.tools.twilio_client import TwilioClient


@pytest.fixture
def http(monkeypatch):
    """Route every httpx.AsyncClient through a handler; returns (requests list, set_handler)."""
    seen: list[httpx.Request] = []
    state = {"handler": lambda req: httpx.Response(200, json={})}
    real = httpx.AsyncClient

    def handler(req):
        seen.append(req)
        return state["handler"](req)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    return SimpleNamespace(seen=seen, set=lambda h: state.__setitem__("handler", h))


def form(req: httpx.Request) -> dict:
    return dict(httpx.QueryParams(req.content.decode()))


# ------------------------------------------------------------------ Twilio
async def test_twilio_disabled_sends_nothing(settings, http):
    t = TwilioClient(settings)
    assert not t.enabled
    assert await t.send_message("+919346968532", "hi") is False
    assert http.seen == []


async def test_twilio_whatsapp_then_sms_fallback_and_call_control(settings, http):
    settings.twilio_account_sid, settings.twilio_auth_token = "AC1", "tok"
    settings.twilio_whatsapp_from, settings.twilio_phone_number = "whatsapp:+14155238886", "+15550001111"
    t = TwilioClient(settings)
    assert await t.send_message("", "hi") is False

    http.set(lambda req: httpx.Response(400, text="bad") if "whatsapp" in form(req)["To"] else
             httpx.Response(201, json={"sid": "SM1"}))
    assert await t.send_message("+919346968532", "Booked") is True
    wa, sms = http.seen[-2:]
    assert form(wa)["To"] == "whatsapp:+919346968532" and form(sms)["To"] == "+919346968532"
    assert wa.url.path == "/2010-04-01/Accounts/AC1/Messages.json"
    assert wa.headers["Authorization"].startswith("Basic ")

    http.set(lambda req: httpx.Response(200, json={"sid": "CA1"}))
    assert await t.transfer_call("CA1", "+919000000000", "Connecting <you>") is True
    twiml = form(http.seen[-1])["Twiml"]
    assert "<Dial>+919000000000</Dial>" in twiml and "&lt;you&gt;" in twiml  # announcement is escaped
    assert await t.hangup_call("CA1") is True and form(http.seen[-1])["Status"] == "completed"

    http.set(lambda req: httpx.Response(500, text="down"))
    assert await t.hangup_call("CA1") is False


# ------------------------------------------------------------------ Meta WhatsApp Cloud API
async def test_meta_whatsapp_text_template_and_errors(settings, http):
    settings.meta_wa_token, settings.meta_wa_phone_id = "EAAtoken", "12345"
    ch = MetaWhatsApp(settings)
    assert ch.enabled()
    assert await ch.send("+919346968532", Message(text="Token 4 for Dr. Rao")) == (True, None)
    req = http.seen[-1]
    body = json.loads(req.content)
    assert req.url.path == "/v21.0/12345/messages" and req.headers["Authorization"] == "Bearer EAAtoken"
    assert body["to"] == "919346968532" and body["text"]["body"] == "Token 4 for Dr. Rao"

    settings.meta_wa_template, settings.meta_wa_template_lang = "vaanidesk_update", "en"
    await ch.send("+919346968532", Message(text="Booked"))
    tpl = json.loads(http.seen[-1].content)["template"]
    assert tpl["name"] == "vaanidesk_update" and tpl["components"][0]["parameters"][0]["text"] == "Booked"

    http.set(lambda req: httpx.Response(400, json={"error": {"message": "template not approved"}}))
    ok, err = await ch.send("+919346968532", Message(text="x"))
    assert ok is False and "400" in err and "template not approved" in err

    def boom(req):
        raise httpx.ConnectError("no route")
    http.set(boom)
    ok, err = await ch.send("+919346968532", Message(text="x"))
    assert ok is False and "unreachable" in err


def test_notifier_prefers_meta_whatsapp_when_configured(settings):
    settings.twilio_account_sid, settings.twilio_auth_token = "AC1", "tok"
    settings.twilio_whatsapp_from = "whatsapp:+14155238886"
    assert type(Notifier(settings, TwilioClient(settings)).channels["whatsapp"]).__name__ == "TwilioWhatsApp"
    settings.meta_wa_token, settings.meta_wa_phone_id = "t", "1"
    assert isinstance(Notifier(settings, TwilioClient(settings)).channels["whatsapp"], MetaWhatsApp)


# ------------------------------------------------------------------ SMTP email
class FakeSMTP:
    instances: list[FakeSMTP] = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, em):
        if FakeSMTP.fail:
            raise smtplib.SMTPRecipientsRefused({em["To"]: (550, b"no such user")})
        self.calls.append(("send", em["To"], em["Subject"]))


async def test_smtp_email_send_and_failure(settings, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances.clear()
    settings.smtp_host, settings.smtp_port, settings.smtp_from = "smtp.gmail.com", 587, "clinic@gmail.com"
    settings.smtp_user, settings.smtp_password = "clinic@gmail.com", "app-password"
    ch = SmtpEmail(settings)
    ok, err = await ch.send("p@example.com", Message(text="Booked", subject="Appointment booked"))
    assert (ok, err) == (True, None)
    smtp = FakeSMTP.instances[-1]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert smtp.calls == ["starttls", ("login", "clinic@gmail.com"), ("send", "p@example.com", "Appointment booked")]

    settings.smtp_starttls, settings.smtp_user = False, ""
    await ch.send("p@example.com", Message(text="x"))
    assert FakeSMTP.instances[-1].calls == [("send", "p@example.com", "Message from your clinic")]

    FakeSMTP.fail = True
    try:
        ok, err = await ch.send("p@example.com", Message(text="x"))
    finally:
        FakeSMTP.fail = False
    assert ok is False and err.startswith("Email failed")


# ------------------------------------------------------------------ Google Calendar HTTP layer
def google(db, business):
    cal = GoogleCalendarSync.__new__(GoogleCalendarSync)
    LocalCalendar.__init__(cal, db, business)
    cal.calendar_id = "clinic@group.calendar.google.com"
    cal._cal_path = f"{GoogleCalendarSync.API}/calendars/clinic%40group.calendar.google.com"
    cal._creds = SimpleNamespace(valid=True, token="ya29.tok", service_account_email="bot@p.iam.gserviceaccount.com")
    return cal


async def test_google_calendar_requests_and_outages(db, business, monday, at, http):
    cal = google(db, business)
    http.set(lambda req: httpx.Response(200, json={"summary": "Sunrise OPD"}))
    assert await cal.verify() == "Sunrise OPD"
    assert http.seen[-1].headers["Authorization"] == "Bearer ya29.tok"

    http.set(lambda req: httpx.Response(404, text="Not Found"))
    with pytest.raises(RuntimeError, match="Google Calendar GET 404"):
        await cal.verify()

    # a calendar outage never blocks bookings: busy = nothing, booking still saved, cancel still works
    http.set(lambda req: httpx.Response(503, text="backend error"))
    assert await cal.external_busy(at(monday, "09:00"), at(monday, "18:00")) == []
    appt = await cal.book(patient_name="Sai", start=at(monday, "18:00"), provider_id="dr-rao", phone="+91999")
    assert appt.id and appt.external_event_id is None
    assert (await cal.cancel(appt.id)).status == "cancelled"

    # calendar not shared with the robot: Google answers with per-calendar errors and no busy list
    http.set(lambda req: httpx.Response(200, json={"calendars": {cal.calendar_id: {"errors": [{"reason": "notFound"}]}}}))
    assert await cal.external_busy(at(monday, "09:00"), at(monday, "18:00")) == []

    # event delete failing is logged, the cancellation still stands
    http.set(lambda req: httpx.Response(200, json={"id": "evt9"}) if req.method == "POST" else httpx.Response(500))
    appt = await cal.book(patient_name="Ravi", start=at(monday, "18:20"), provider_id="dr-rao", phone="+91999")
    assert (await db.get_appointment(appt.id)).external_event_id == "evt9"
    assert (await cal.cancel(appt.id)).status == "cancelled"
    assert http.seen[-1].method == "DELETE" and http.seen[-1].url.path.endswith("/events/evt9")


# ------------------------------------------------------------------ Sarvam TTS: REST fallback and stream errors
def wav(rate=16000, samples=160) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(b"\x01\x00" * samples)
    return buf.getvalue()


async def collect(stream) -> bytes:
    return b"".join([c async for c in stream.audio()])


async def test_sarvam_rest_tts(http):
    http.set(lambda req: httpx.Response(200, json={"audios": [base64.b64encode(wav()).decode()]}))
    stream = await tts_sarvam.SarvamTTS("key", mode="rest").open("te-IN", 16000)
    await stream.send_text("  ")  # blank text is skipped
    await stream.send_text("నమస్కారం")
    await stream.finish()
    assert len(await collect(stream)) == 320
    body = json.loads(http.seen[0].content)
    assert len(http.seen) == 1 and body["target_language_code"] == "te-IN" and body["speech_sample_rate"] == 16000
    assert http.seen[0].headers["api-subscription-key"] == "key"
    await stream.close()

    http.set(lambda req: httpx.Response(429, text="rate limited"))
    stream = await tts_sarvam.SarvamTTS("key", mode="rest").open("hi-IN", 16000)
    await stream.send_text("नमस्ते")
    await stream.finish()
    assert await collect(stream) == b""  # error ends the audio instead of hanging the call


class FakeWS:
    def __init__(self, messages):
        self.messages, self.sent, self.closed = messages, [], False

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.messages:
            yield json.dumps(m)

    async def close(self):
        self.closed = True


async def test_sarvam_stream_skips_mp3_and_stops_on_error():
    ws = FakeWS([
        {"type": "audio", "data": {"audio": base64.b64encode(b"ID3...").decode(), "content_type": "audio/mpeg"}},
        {"type": "audio", "data": {"audio": base64.b64encode(wav(8000, 80)).decode(), "content_type": "audio/wav"}},
        {"type": "error", "data": {"message": "quota exceeded"}},
        {"type": "audio", "data": {"audio": base64.b64encode(wav()).decode(), "content_type": "audio/wav"}},
    ])
    stream = tts_sarvam.SarvamStreamTTS(ws, 16000)
    await stream.send_text("hello")
    await stream.finish()
    audio = await collect(stream)
    assert len(audio) == 320  # only the 8 kHz WAV chunk, resampled to 16 kHz; mp3 dropped; stops at the error
    assert [m["type"] for m in ws.sent] == ["text", "flush"]
    await stream.close()
    assert ws.closed
    await stream.send_text("after close")  # ignored, no crash
    assert len(ws.sent) == 2
