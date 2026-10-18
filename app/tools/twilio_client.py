"""Minimal async Twilio REST client: SMS/WhatsApp messages and live call control."""
from __future__ import annotations

import logging
from xml.sax.saxutils import escape

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class TwilioClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}"

    @property
    def enabled(self) -> bool:
        return self.s.twilio_enabled

    async def _post(self, path: str, data: dict) -> dict | None:
        if not self.enabled:
            log.info("[twilio disabled] POST %s %s", path, data)
            return None
        async with httpx.AsyncClient(timeout=10, auth=(self.s.twilio_account_sid, self.s.twilio_auth_token)) as c:
            r = await c.post(f"{self.base}{path}", data=data)
            if r.status_code >= 400:
                log.error("Twilio %s failed (%s): %s", path, r.status_code, r.text)
                return None
            return r.json()

    async def send_message(self, to: str, body: str) -> bool:
        """Send WhatsApp if configured, else SMS."""
        if not to:
            return False
        if self.s.twilio_whatsapp_from:
            res = await self._post("/Messages.json", {"From": self.s.twilio_whatsapp_from,
                                                      "To": f"whatsapp:{to}", "Body": body})
            if res:
                return True
        if self.s.twilio_phone_number:
            return bool(await self._post("/Messages.json", {"From": self.s.twilio_phone_number,
                                                            "To": to, "Body": body}))
        return False

    async def transfer_call(self, call_sid: str, number: str, announcement: str = "") -> bool:
        twiml = "<Response>"
        if announcement:
            twiml += f"<Say>{escape(announcement)}</Say>"
        twiml += f"<Dial>{escape(number)}</Dial></Response>"
        return bool(await self._post(f"/Calls/{call_sid}.json", {"Twiml": twiml}))

    async def hangup_call(self, call_sid: str) -> bool:
        return bool(await self._post(f"/Calls/{call_sid}.json", {"Status": "completed"}))
