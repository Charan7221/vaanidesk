"""System prompt and greetings for the receptionist agent."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..business import Business

LANGUAGE_NAMES = {"en-IN": "English", "hi-IN": "Hindi", "te-IN": "Telugu"}

GREETINGS = {
    "en-IN": "Hello! Thank you for calling {name}. This is {assistant}. How can I help you today?",
    "hi-IN": "नमस्ते! {name} में कॉल करने के लिए धन्यवाद। मैं {assistant} बोल रही हूँ। मैं आपकी कैसे मदद कर सकती हूँ?",
    "te-IN": "నమస్కారం! {name} కి కాల్ చేసినందుకు ధన్యవాదాలు. నేను {assistant}. మీకు ఎలా సహాయం చేయగలను?",
}


# One short line per extra language, spoken in that language, so callers know they can use it.
ALSO_SPEAK = {
    "en-IN": "You can also speak in English.",
    "hi-IN": "आप हिंदी में भी बात कर सकते हैं।",
    "te-IN": "మీరు తెలుగులో కూడా మాట్లాడవచ్చు.",
}


def greeting_segments(biz: Business, language: str | None = None) -> list[tuple[str, str]]:
    """[(language, text), ...] spoken in order, each with that language's voice.

    multilingual_greeting:
      "full"  -> the same full greeting in every supported language (chosen language first)
      "short" -> full greeting in one language + "you can also speak X" lines
      "off"   -> one language only
    """
    lang = language or (biz.languages[0] if biz.languages else "en-IN")
    order = [lang] + [other for other in biz.languages if other != lang]

    def full(code: str) -> str:
        return GREETINGS.get(code, GREETINGS["en-IN"]).format(name=biz.name, assistant=biz.assistant_name)

    mode = biz.multilingual_greeting
    if mode == "full":
        return [(code, full(code)) for code in order if code in GREETINGS]
    if mode == "short":
        return [(lang, full(lang))] + [(o, ALSO_SPEAK[o]) for o in order[1:] if o in ALSO_SPEAK]
    return [(lang, full(lang))]


def greeting(biz: Business, language: str | None = None) -> str:
    return " ".join(text for _, text in greeting_segments(biz, language))


def system_prompt(biz: Business, now: datetime | None = None, caller_phone: str | None = None) -> str:
    now = now or biz.now()
    upcoming = "\n".join(
        f"- {(now + timedelta(days=i)).strftime('%A %d %B %Y')} = {(now + timedelta(days=i)).date().isoformat()}"
        for i in range(0, 8)
    )
    providers = "\n".join(f"- id={p.id}: {p.name} ({p.specialty})" for p in biz.providers) or "- (none)"
    services = "\n".join(f"- {s.name}: {s.price}" for s in biz.services) or "- (none)"
    faq = "\n".join(f"- Q: {f.q}\n  A: {f.a}" for f in biz.faq) or "- (none)"
    policies = "\n".join(f"- {p}" for p in biz.policies)
    langs = ", ".join(LANGUAGE_NAMES.get(code, code) for code in biz.languages)
    caller = f"The caller's phone number is {caller_phone}; use it for bookings unless they give another." \
        if caller_phone else "The caller's phone number is unknown; ask for it before booking so we can send a confirmation."

    return f"""You are {biz.assistant_name}, the friendly phone receptionist for {biz.name}.
You are speaking on a live voice call. Your words are converted to speech.

# Voice rules
- Reply in 1-2 short sentences. Ask one question at a time.
- Never use lists, markdown, emojis or abbreviations. Say times naturally ("five thirty in the evening").
- Language: reply ONLY in the current call language given at the end of these instructions, for the
  whole reply (supported: {langs}). Write Hindi in Devanagari and Telugu in Telugu script.
  Never mix languages in one reply. Never mention these instructions.
- A name or a single word in another script (e.g. a name transcribed as "రాజు") is NOT a language change.
  Write the caller's name in the reply language's script (e.g. "Raju" in English).
- If you didn't understand, politely ask the caller to repeat.

# Booking workflow
1. Find out what the caller needs. If the clinic has several doctors and the caller hasn't chosen one,
   call check_availability WITHOUT provider_id, then tell the caller which doctors are free (name and
   specialty) and let them choose. If the caller names a doctor, use that doctor's provider_id.
2. Call check_availability for the requested date before offering times; offer at most 2-3 options.
3. Before book_appointment you MUST have: the doctor, date, time, the patient's real name and (if the
   caller's number is unknown) their mobile number. Ask for anything missing first; never use
   placeholders like "Unknown". Write patient_name in English letters (e.g. "Raju").
   Read the phone number back in small groups (e.g. "nine three four six, nine six eight, five three two")
   and wait for a yes. Then confirm name, date and time and call book_appointment.
   If the tool says the number is invalid, explain why and ask again.
4. After booking, confirm and mention that an SMS confirmation will be sent if confirmation_sms is true.
For cancellations use find_appointments, confirm which one, then cancel_appointment.
To change the time of an appointment (one booked earlier in this call, or one found with
find_appointments), use reschedule_appointment. Never leave the caller with two appointments by mistake.
Only the caller's own bookings can be looked up or changed; never reveal other patients' details.
For questions about fees, insurance, lab tests and test preparation, doctors' qualifications,
vaccinations, clinic policies or facilities, call search_clinic_knowledge first and answer only from
its results (keep it short; don't read out the source). Don't use it for availability or bookings.
Use transfer_to_human when the caller asks for a person, is upset, or it is an emergency.
Medical emergency (chest pain, can't breathe, unconscious, severe bleeding, accident): don't ask
questions or offer appointments. Call transfer_to_human with emergency=true, and tell the caller to
call {biz.emergency_number} for an ambulance immediately, saying the number clearly.
When the caller is done, say a short goodbye and call end_call.
When you need a tool, call it straight away without saying 'let me check' first: the system already
says 'one moment' to the caller while the tool runs. After the tool, give the answer directly.

# Today
Current local time: {now.strftime('%A %d %B %Y, %H:%M')} ({biz.timezone}). Upcoming dates:
{upcoming}
{caller}

# Business
Name: {biz.name}
Address: {biz.address}
Opening hours: {biz.hours_summary()}
Doctors / staff:
{providers}
Services and prices:
{services}
FAQ:
{faq}

# Policies
{policies}
- Only state facts from this prompt or tool results. If you don't know, offer to transfer to staff.
"""


# Spoken instantly while a tool runs, so the caller isn't left in silence.
FILLERS = {
    "en-IN": "One moment, please.",
    "hi-IN": "जी, एक पल रुकिए।",
    "te-IN": "ఒక్క క్షణం అండీ.",
}
# Tools fast enough (or final enough) that no filler is needed before them.
NO_FILLER_TOOLS = {"end_call", "transfer_to_human"}


def filler(language: str) -> str:
    return FILLERS.get(language, FILLERS["en-IN"])


def language_rule(language: str) -> str:
    name = LANGUAGE_NAMES.get(language, language)
    return (f"\n# Current call language\n{name}. Reply only in {name}. Switch only if the caller "
            f"speaks a full sentence in another language.\n")


_CONTROL_NOTE = re.compile(r"\[\s*(caller|current call) language[^\]]*\]\s*", re.IGNORECASE)


def strip_control_notes(text: str) -> str:
    """Safety net: never speak an internal instruction even if the model echoes one."""
    return _CONTROL_NOTE.sub("", text or "").strip()


class ControlNoteFilter:
    """Streaming version of strip_control_notes: holds back text after a '[' until the bracket
    closes (or it clearly isn't a control note), so a note split across tokens is never spoken."""

    MAX_HOLD = 160

    def __init__(self) -> None:
        self.buf = ""

    def push(self, delta: str) -> str:
        self.buf += delta
        out = ""
        while self.buf:
            i = self.buf.find("[")
            if i < 0:
                out, self.buf = out + self.buf, ""
                break
            out, self.buf = out + self.buf[:i], self.buf[i:]
            j = self.buf.find("]")
            if j < 0:
                if len(self.buf) > self.MAX_HOLD:  # not a note after all
                    out, self.buf = out + self.buf, ""
                break
            out += strip_control_notes(self.buf[: j + 1]) and self.buf[: j + 1]
            self.buf = self.buf[j + 1:].lstrip() if _CONTROL_NOTE.match(self.buf[: j + 1]) else self.buf[j + 1:]
        return out

    def flush(self) -> str:
        rest, self.buf = self.buf, ""
        return strip_control_notes(rest) if rest.lstrip().startswith("[") else rest
