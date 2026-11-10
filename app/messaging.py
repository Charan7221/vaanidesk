"""Messages to patients (and staff): WhatsApp, SMS and email, with fallback and a delivery log.

Channels (each switches on when its settings are present in .env):
  * WhatsApp: Meta WhatsApp Cloud API (META_WA_*) or Twilio WhatsApp (TWILIO_WHATSAPP_FROM)
  * SMS:      Twilio SMS (TWILIO_PHONE_NUMBER). Indian numbers need DLT-registered templates.
  * Email:    any SMTP server (SMTP_*): Gmail app password, Zoho, SendGrid, Amazon SES, ...

For a phone number, the clinic's ``notifications.phone_channels`` are tried in order (default WhatsApp, then
SMS) and the first one that is accepted wins. Email is sent as well when the patient gave an address.
Every attempt is written to the message log shown on the dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from html import escape

import httpx

from .config import Settings

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def valid_email(value: str | None) -> str | None:
    value = (value or "").strip().lower().replace(" at ", "@").replace(" dot ", ".").replace(" ", "")
    return value if EMAIL_RE.match(value) else None


@dataclass
class Message:
    text: str  # the WhatsApp / SMS body (also the plain-text email)
    subject: str = ""
    title: str = ""  # email heading
    lines: list[tuple[str, str]] = field(default_factory=list)  # email details table
    footer: str = ""
    log_as: str | None = None  # what the message log shows instead of the text (e.g. one-time codes)

    @property
    def log_text(self) -> str:
        return self.log_as if self.log_as is not None else self.text


class Channel:
    name = "?"

    def enabled(self) -> bool:
        raise NotImplementedError

    async def send(self, to: str, msg: Message) -> tuple[bool, str | None]:
        raise NotImplementedError


class TwilioWhatsApp(Channel):
    name = "whatsapp"

    def __init__(self, twilio, s: Settings):
        self.twilio, self.s = twilio, s

    def enabled(self) -> bool:
        return self.twilio.enabled and bool(self.s.twilio_whatsapp_from)

    async def send(self, to, msg):
        ok = await self.twilio._post("/Messages.json", {"From": self.s.twilio_whatsapp_from, "To": f"whatsapp:{to}",
                                                         "Body": msg.text})
        return bool(ok), None if ok else "Twilio WhatsApp rejected the message (see server log)"


class MetaWhatsApp(Channel):
    """WhatsApp Cloud API. Outside the 24-hour customer-service window Meta only delivers approved templates,
    so set META_WA_TEMPLATE to a template whose body is a single variable, e.g. "{{1}}" (category Utility)."""

    name = "whatsapp"

    def __init__(self, s: Settings):
        self.s = s

    def enabled(self) -> bool:
        return bool(self.s.meta_wa_token and self.s.meta_wa_phone_id)

    async def send(self, to, msg):
        payload: dict = {"messaging_product": "whatsapp", "to": to.lstrip("+")}
        if self.s.meta_wa_template:
            payload |= {"type": "template", "template": {
                "name": self.s.meta_wa_template, "language": {"code": self.s.meta_wa_template_lang},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": msg.text[:1000]}]}]}}
        else:
            payload |= {"type": "text", "text": {"body": msg.text}}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"https://graph.facebook.com/v21.0/{self.s.meta_wa_phone_id}/messages",
                                 json=payload, headers={"Authorization": f"Bearer {self.s.meta_wa_token}"})
        except httpx.HTTPError as exc:
            return False, f"WhatsApp Cloud API unreachable: {exc}"
        if r.status_code >= 400:
            return False, f"WhatsApp Cloud API {r.status_code}: {r.text[:300]}"
        return True, None


class TwilioSMS(Channel):
    name = "sms"

    def __init__(self, twilio, s: Settings):
        self.twilio, self.s = twilio, s

    def enabled(self) -> bool:
        return self.twilio.enabled and bool(self.s.twilio_phone_number)

    async def send(self, to, msg):
        ok = await self.twilio._post("/Messages.json", {"From": self.s.twilio_phone_number, "To": to, "Body": msg.text})
        return bool(ok), None if ok else "Twilio SMS rejected the message (see server log)"


class SmtpEmail(Channel):
    name = "email"

    def __init__(self, s: Settings):
        self.s = s

    def enabled(self) -> bool:
        return bool(self.s.smtp_host and self.s.smtp_from)

    def _build(self, to: str, msg: Message) -> EmailMessage:
        em = EmailMessage()
        em["From"], em["To"], em["Subject"] = self.s.smtp_from, to, msg.subject or msg.title or "Message from your clinic"
        em.set_content(msg.text)
        em.add_alternative(render_html(msg), subtype="html")
        return em

    def _send_sync(self, em: EmailMessage) -> None:
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
            if self.s.smtp_starttls:
                smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(em)

    async def send(self, to, msg):
        try:
            await asyncio.to_thread(self._send_sync, self._build(to, msg))
            return True, None
        except (smtplib.SMTPException, OSError) as exc:
            return False, f"Email failed: {exc}"


def render_html(msg: Message) -> str:
    """A small, clean email that renders in Gmail / Outlook (inline styles only)."""
    rows = "".join(f'<tr><td style="padding:6px 16px 6px 0;color:#5f6b76">{escape(k)}</td>'
                   f'<td style="padding:6px 0;font-weight:600;color:#0f1b24">{escape(v)}</td></tr>'
                   for k, v in msg.lines if v)
    body = escape(msg.text).replace("\n", "<br>") if not msg.lines else ""
    return f"""<!doctype html><html><body style="margin:0;background:#f5f7f8;font-family:Arial,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" style="max-width:520px;background:#ffffff;border-radius:14px;border:1px solid #e2e7eb">
<tr><td style="background:#0f7b74;color:#ffffff;padding:18px 22px;border-radius:14px 14px 0 0;font-size:18px;font-weight:700">
{escape(msg.title or msg.subject)}</td></tr>
<tr><td style="padding:20px 22px;font-size:15px;line-height:1.5;color:#0f1b24">{body}
<table role="presentation" cellpadding="0" cellspacing="0">{rows}</table>
{f'<p style="margin:16px 0 0;color:#5f6b76;font-size:13px">{escape(msg.footer)}</p>' if msg.footer else ''}
</td></tr></table>
<p style="color:#93a1ab;font-size:11px;margin:12px 0 0">Sent by VaaniDesk on behalf of your clinic.</p>
</td></tr></table></body></html>"""


class Notifier:
    def __init__(self, settings: Settings, twilio, db=None):
        self.s, self.db = settings, db
        meta = MetaWhatsApp(settings)
        self.channels: dict[str, Channel] = {
            "whatsapp": meta if meta.enabled() else TwilioWhatsApp(twilio, settings),
            "sms": TwilioSMS(twilio, settings),
            "email": SmtpEmail(settings),
        }

    def status(self) -> dict[str, bool]:
        return {name: ch.enabled() for name, ch in self.channels.items()}

    @property
    def enabled(self) -> bool:
        return any(self.status().values())

    async def _log(self, kind, channel, to, status, preview, error=None):
        if self.db is not None:
            try:
                await self.db.log_message(kind, channel, to, status, preview, error)
            except Exception:
                log.exception("could not write the message log")

    async def send(self, *, kind: str, msg: Message, phone: str | None = None, email: str | None = None,
                   phone_channels: list[str] | None = None, use_email: bool = True) -> dict:
        """Send to a phone (first channel that works) and/or an email. Returns {"sent": [...], "failed": [...]}."""
        out: dict = {"sent": [], "failed": []}
        if phone:
            tried = False
            for name in phone_channels or ["whatsapp", "sms"]:
                ch = self.channels.get(name)
                if not ch or not ch.enabled():
                    continue
                tried = True
                ok, err = await ch.send(phone, msg)
                await self._log(kind, name, phone, "sent" if ok else "failed", msg.log_text, err)
                if ok:
                    out["sent"].append(name)
                    break
                out["failed"].append(name)
            if not tried:
                await self._log(kind, "none", phone, "not_configured", msg.log_text,
                                "No WhatsApp/SMS provider is set up (see .env: TWILIO_* or META_WA_*)")
        email = valid_email(email) if email else None
        if email and use_email:
            ch = self.channels["email"]
            if ch.enabled():
                ok, err = await ch.send(email, msg)
                await self._log(kind, "email", email, "sent" if ok else "failed", msg.log_text, err)
                (out["sent"] if ok else out["failed"]).append("email")
            else:
                await self._log(kind, "none", email, "not_configured", msg.log_text, "No email server is set up (SMTP_*)")
        return out

    async def notify_patient(self, biz, *, kind: str, msg: Message, phone: str | None, email: str | None) -> dict:
        """Patient messages follow the clinic's notification settings."""
        n = biz.notifications
        if not n.confirmations and kind not in ("reminder", "manual", "pilot"):
            return {"sent": [], "failed": [], "skipped": "confirmations are switched off"}
        return await self.send(kind=kind, msg=msg, phone=phone, email=email, phone_channels=n.phone_channels,
                               use_email=n.email)

    async def send_message(self, to: str, body: str) -> bool:
        """Compatibility helper for short staff alerts."""
        return bool((await self.send(kind="alert", msg=Message(text=body), phone=to))["sent"])
