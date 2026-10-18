"""Unit tests: audio codecs, sentence chunking, language detection, business hours, security."""
from __future__ import annotations

import struct

import numpy as np

from app.audio import pcm16_to_ulaw, resample_pcm16, strip_wav_header, ulaw_to_pcm16
from app.pipeline.prompts import greeting, system_prompt
from app.pipeline.text import SentenceChunker, detect_script_language
from app.security import is_valid_twilio_request, twilio_signature


# ---------------------------------------------------------------- audio
def test_ulaw_roundtrip_is_close():
    t = np.linspace(0, 1, 8000, endpoint=False)
    pcm = (np.sin(2 * np.pi * 440 * t) * 12000).astype("<i2").tobytes()
    ulaw = pcm16_to_ulaw(pcm)
    assert len(ulaw) == 8000
    back = np.frombuffer(ulaw_to_pcm16(ulaw), dtype="<i2").astype(np.int32)
    orig = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
    # mu-law is lossy (~2-3% of amplitude); check relative error
    assert np.max(np.abs(back - orig)) < 12000 * 0.04


def test_ulaw_known_values():
    assert ulaw_to_pcm16(bytes([0xFF])) == struct.pack("<h", 0)
    assert pcm16_to_ulaw(struct.pack("<h", 0)) == bytes([0xFF])
    assert struct.unpack("<h", ulaw_to_pcm16(bytes([0x00])))[0] == -32124


def test_resample_lengths():
    pcm = b"\x00\x00" * 8000
    assert len(resample_pcm16(pcm, 8000, 16000)) == 32000
    assert len(resample_pcm16(pcm, 16000, 8000)) == 8000
    assert resample_pcm16(pcm, 8000, 8000) is pcm


def test_strip_wav_header():
    pcm = b"\x01\x02" * 100
    header = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 22050,
              44100, 2, 16) + b"data" + struct.pack("<I", len(pcm)))
    data, rate = strip_wav_header(header + pcm)
    assert data == pcm and rate == 22050
    assert strip_wav_header(pcm) == (pcm, None)


# ---------------------------------------------------------------- text
def test_chunker_emits_sentences_early():
    c = SentenceChunker()
    out = []
    for tok in ["Sure", ", I can", " help. Dr", ". Rao is free at 5", ".30 today", ". Shall", " I book?"]:
        out += c.push(tok)
    assert out == ["Sure, I can help.", "Dr. Rao is free at 5.30 today."]
    assert c.flush() == "Shall I book?"


def test_chunker_handles_indic_danda_and_markdown():
    c = SentenceChunker()
    assert c.push("**नमस्ते**। आप कैसे हैं? ") == ["नमस्ते।", "आप कैसे हैं?"]


def test_chunker_splits_long_clauses_at_commas():
    c = SentenceChunker(soft_limit=60)
    out = c.push("We have slots at nine in the morning, ten thirty in the morning, and several in the evening too")
    assert out and out[0].endswith(",")


def test_detect_script_language():
    assert detect_script_language("మీకు ఎలా సహాయం చేయగలను?") == "te-IN"
    assert detect_script_language("आपका अपॉइंटमेंट बुक हो गया है") == "hi-IN"
    assert detect_script_language("Your appointment is booked.") == "en-IN"
    assert detect_script_language("12:30", default="te-IN") == "te-IN"


# ---------------------------------------------------------------- business / prompts
def test_slots_follow_opening_hours(business, monday):
    slots = business.candidate_slots(monday)
    assert slots[0].strftime("%H:%M") == "09:00"
    assert slots[-1].strftime("%H:%M") == "19:40"
    assert all(s.strftime("%H:%M") not in ("13:00", "16:40") for s in slots)
    sunday = monday.replace(day=monday.day)  # copy
    while sunday.weekday() != 6:
        sunday = sunday.fromordinal(sunday.toordinal() + 1)
    assert business.candidate_slots(sunday) == []


def test_prompt_and_greeting(business):
    prompt = system_prompt(business, caller_phone="+911234567890")
    assert business.name in prompt and "+911234567890" in prompt and "dr-rao" in prompt
    assert "నమస్కారం" in greeting(business, "te-IN")
    assert business.name in greeting(business, "en-IN")


# ---------------------------------------------------------------- security
def test_twilio_signature_validation():
    params = {"CallSid": "CA123", "From": "+15005550006", "To": "+15005550001"}
    url = "https://example.ngrok.app/twilio/voice"
    sig = twilio_signature("token123", url, params)
    assert is_valid_twilio_request("token123", url, params, sig)
    assert not is_valid_twilio_request("token123", url, {**params, "From": "+1999"}, sig)
    assert not is_valid_twilio_request("other", url, params, sig)
    assert not is_valid_twilio_request("token123", url, params, None)


def test_normalize_phone():
    from app.phone import normalize_phone

    assert normalize_phone("9346968532") == "+919346968532"
    assert normalize_phone("093469 68532") == "+919346968532"
    assert normalize_phone("+91 93469-68532") == "+919346968532"
    assert normalize_phone("919346968532") == "+919346968532"
    assert normalize_phone("+14155550123") == "+14155550123"
    assert normalize_phone("") is None and normalize_phone(None) is None


def test_looks_unfinished_and_strip_notes():
    from app.pipeline.prompts import strip_control_notes
    from app.pipeline.text import looks_unfinished

    assert looks_unfinished("I want to book appointment for.")
    assert not looks_unfinished("Tomorrow morning.")
    assert strip_control_notes("[Caller language: English. Reply only in English.] Hello!") == "Hello!"
    assert strip_control_notes("Hello!") == "Hello!"


def test_control_note_filter_streaming():
    from app.pipeline.prompts import ControlNoteFilter

    f = ControlNoteFilter()
    tokens = ["[Caller ", "language: English. ", "Reply only in English.] ", "Hello! ", "Room [2] is free."]
    out = "".join(f.push(t) for t in tokens) + f.flush()
    assert out == "Hello! Room [2] is free."


def test_greeting_modes(business):
    from app.pipeline.prompts import greeting_segments

    full = greeting_segments(business, "te-IN")
    assert [lang for lang, _ in full] == ["te-IN", "en-IN", "hi-IN"]  # chosen language first
    assert full[0][1].startswith("నమస్కారం!") and full[1][1].startswith("Hello!") and full[2][1].startswith("नमस्ते!")
    short = greeting_segments(business.model_copy(update={"multilingual_greeting": "short"}), "en-IN")
    assert short[1] == ("hi-IN", "आप हिंदी में भी बात कर सकते हैं।")
    off = greeting_segments(business.model_copy(update={"multilingual_greeting": "off"}), "en-IN")
    assert len(off) == 1
