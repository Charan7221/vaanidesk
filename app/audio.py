"""Small, dependency-free audio helpers (numpy only).

Twilio Media Streams use 8 kHz G.711 mu-law; browsers send 16 kHz PCM16.
Python 3.13 removed the stdlib ``audioop`` module, so the codec is implemented here.
"""
from __future__ import annotations

import struct

import numpy as np

_BIAS = 0x84
_CLIP = 32635


def _build_ulaw_decode_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        table[i] = -sample if sign else sample
    return table


_ULAW_DECODE = _build_ulaw_decode_table()


def ulaw_to_pcm16(data: bytes) -> bytes:
    """Decode G.711 mu-law bytes to little-endian 16-bit PCM."""
    idx = np.frombuffer(data, dtype=np.uint8)
    return _ULAW_DECODE[idx].astype("<i2").tobytes()


def pcm16_to_ulaw(data: bytes) -> bytes:
    """Encode little-endian 16-bit PCM to G.711 mu-law bytes."""
    pcm = np.frombuffer(data, dtype="<i2").astype(np.int32)
    sign = np.where(pcm < 0, 0x80, 0x00)
    mag = np.minimum(np.abs(pcm), _CLIP) + _BIAS
    exponent = np.floor(np.log2(np.maximum(mag, 1))).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    ulaw = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8).tobytes()


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resampler for mono PCM16 (good enough for speech)."""
    if src_rate == dst_rate or not data:
        return data
    pcm = np.frombuffer(data, dtype="<i2").astype(np.float32)
    n_out = int(round(len(pcm) * dst_rate / src_rate))
    if n_out == 0:
        return b""
    x_old = np.linspace(0.0, 1.0, num=len(pcm), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    out = np.interp(x_new, x_old, pcm)
    return np.clip(out, -32768, 32767).astype("<i2").tobytes()


def strip_wav_header(data: bytes) -> tuple[bytes, int | None]:
    """If ``data`` is a WAV file, return (pcm_bytes, sample_rate); else (data, None)."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data, None
    pos, rate = 12, None
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = pos + 8
        if chunk_id == b"fmt ":
            rate = struct.unpack("<I", data[body + 4 : body + 8])[0]
        elif chunk_id == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) else min(len(data), body + size)
            return data[body:end], rate
        pos = body + size + (size & 1)
    return b"", rate


def pcm16_duration_ms(data: bytes, rate: int) -> float:
    return len(data) / 2 / rate * 1000.0
