"""Text utilities for streaming speech: sentence chunking and script-based language detection."""
from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"([.!?।॥]+[\"')\]]*)(\s+|$)")
_MARKDOWN = re.compile(r"[*_#`>~]+")


_ABBREVIATIONS = ("dr.", "mr.", "mrs.", "ms.", "st.", "no.", "approx.", "e.g.", "i.e.")


class SentenceChunker:
    """Accumulates streamed LLM tokens and yields speakable chunks as early as possible.

    Emits on sentence punctuation followed by whitespace (so "5.30" or "Dr. Rao" are not
    split); very long clauses are split at commas so the first audio starts quickly.
    """

    def __init__(self, soft_limit: int = 110, min_chars: int = 2):
        self.buf = ""
        self.soft_limit = soft_limit
        self.min_chars = min_chars

    def _boundary(self) -> int | None:
        for m in _SENTENCE_END.finditer(self.buf):
            if not m.group(2):  # punctuation at the very end: wait for more tokens
                return None
            words = self.buf[: m.end(1)].split()
            if words and words[-1].lower() in _ABBREVIATIONS:
                continue
            return m.end()
        return None

    def push(self, delta: str) -> list[str]:
        self.buf += _MARKDOWN.sub("", delta)
        out: list[str] = []
        while True:
            end = self._boundary()
            if end is not None:
                piece, self.buf = self.buf[:end].strip(), self.buf[end:]
                if len(piece) >= self.min_chars:
                    out.append(piece)
                continue
            if len(self.buf) > self.soft_limit:
                cut = self.buf.rfind(", ", 0, self.soft_limit)
                if cut > 20:
                    piece, self.buf = self.buf[: cut + 1].strip(), self.buf[cut + 1:]
                    out.append(piece)
                    continue
            break
        return out

    def flush(self) -> str | None:
        piece, self.buf = self.buf.strip(), ""
        return piece if len(piece) >= self.min_chars else None


def detect_script_language(text: str, default: str = "en-IN") -> str:
    """Pick a TTS language from the script of the text (Telugu, Devanagari or Latin)."""
    te = sum(1 for c in text if "ఀ" <= c <= "౿")
    hi = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    if te and te >= hi:
        return "te-IN"
    if hi:
        return "hi-IN"
    if latin:
        return "en-IN"
    return default


_UNFINISHED_ENDINGS = {
    # English
    "for", "to", "and", "or", "but", "on", "at", "the", "a", "an", "with", "in", "of", "my", "is", "want",
    "book", "need", "like", "uh", "um", "so", "because",
    # common Hindi / Telugu connectors
    "और", "के", "की", "को", "लिए", "में", "మరియు", "కోసం", "కి",
}


def looks_unfinished(text: str) -> bool:
    """True when an utterance probably stops mid-sentence (e.g. "I want to book an appointment for")."""
    words = re.sub(r"[.,!?।…]+$", "", text.strip()).split()
    return bool(words) and words[-1].lower() in _UNFINISHED_ENDINGS
