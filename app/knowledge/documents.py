"""Turn clinic documents (.md / .txt / .pdf) into small, citable chunks."""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED = {".md", ".markdown", ".txt", ".pdf"}
SKIP = {"readme.md"}
MAX_CHARS = 700  # ~120-150 words: small enough to be precise, big enough to hold one answer


@dataclass
class Chunk:
    source: str  # file name, shown as the citation
    section: str  # heading path ("Lab tests › Reports") or "page 3" for PDFs
    text: str
    page: int | None = None

    @property
    def citation(self) -> str:
        # drop the document title (first heading) for a shorter citation: "lab-tests.md › Reports"
        section = self.section.split(" › ", 1)[-1] if self.section else ""
        return f"{self.source} › {section}" if section else self.source

    @property
    def search_text(self) -> str:
        """What gets embedded / keyword-indexed: the heading path gives each chunk its context."""
        return f"{self.section}\n{self.text}" if self.section else self.text


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split on paragraphs / list items, then sentences, keeping pieces under max_chars."""
    parts, buf = [], ""
    units = [u for u in re.split(r"\n\s*\n|\n(?=\s*[-*] )", text) if u.strip()]
    for unit in units:
        unit = unit.strip()
        if len(unit) > max_chars:  # a very long paragraph: fall back to sentences
            for sent in re.split(r"(?<=[.!?।])\s+", unit):
                if len(buf) + len(sent) + 1 > max_chars and buf:
                    parts.append(buf)
                    buf = ""
                buf = f"{buf} {sent}".strip()
            continue
        if len(buf) + len(unit) + 1 > max_chars and buf:
            parts.append(buf)
            buf = ""
        buf = f"{buf}\n{unit}".strip()
    if buf:
        parts.append(buf)
    return parts


def chunk_markdown(source: str, text: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """One chunk per heading section (split further if long), labelled with its heading path."""
    chunks: list[Chunk] = []
    path: list[str] = []
    body: list[str] = []

    def flush() -> None:
        content = "\n".join(body).strip()
        body.clear()
        if content:
            section = " › ".join(path)
            chunks.extend(Chunk(source, section, piece) for piece in _split_long(content, max_chars))

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[: level - 1] + [m.group(2).strip()]
        else:
            body.append(line)
    flush()
    return chunks


def chunk_pdf(source: str, data: bytes, max_chars: int = MAX_CHARS) -> list[Chunk]:
    from pypdf import PdfReader

    chunks = []
    for n, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
        text = re.sub(r"[ \t]+", " ", page.extract_text() or "").strip()
        chunks.extend(Chunk(source, f"page {n}", piece, page=n) for piece in _split_long(text, max_chars))
    return chunks


def chunk_document(source: str, data: bytes) -> list[Chunk]:
    suffix = Path(source).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported file type {suffix!r}; use .md, .txt or .pdf")
    if suffix == ".pdf":
        return chunk_pdf(source, data)
    return chunk_markdown(source, data.decode("utf-8", errors="replace"))


def folder_documents(folder: Path) -> dict[str, bytes]:
    if not folder.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in SUPPORTED and p.name.lower() not in SKIP}
