"""RAG: chunking, hybrid retrieval, folder sync, the agent tool and the admin API (offline)."""
from __future__ import annotations

import hashlib
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from app.knowledge import Embedder, KnowledgeStore, chunk_document
from app.knowledge.ranking import tokenize
from app.main import create_app
from app.state import AppState
from app.tools.registry import ToolContext

from .fakes import FakeLLM, FakeSTT, FakeTTS, text

KNOWLEDGE = ROOT_DIR / "knowledge"
AUTH = ("admin", "secret")


class HashEmbedder(Embedder):
    """Deterministic bag-of-words vectors standing in for a real embedding model."""

    model = "hash-64"

    def __init__(self):
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            v = np.zeros(64)
            for w in tokenize(t):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
            out.append(v.tolist())
        return out


class BrokenEmbedder(Embedder):
    model = "broken"

    async def embed(self, texts):
        raise RuntimeError("429 quota exceeded")


@pytest.fixture
async def store(db):
    s = KnowledgeStore(db, None)
    await s.sync_folder(KNOWLEDGE)
    return s


def test_markdown_chunks_carry_their_heading_as_citation():
    md = "# Lab\n\n## Reports\nReady by 5 PM.\n\n## Prices\n- CBP: ₹250\n- Sugar: ₹80\n"
    chunks = chunk_document("lab.md", md.encode())
    assert [c.citation for c in chunks] == ["lab.md › Reports", "lab.md › Prices"]
    assert chunks[1].search_text.startswith("Lab › Prices") and "₹250" in chunks[1].text


def test_long_sections_are_split_under_the_size_limit():
    md = "# Policy\n" + "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(6))
    chunks = chunk_document("p.md", md.encode())
    assert len(chunks) > 1 and all(len(c.text) <= 700 for c in chunks)


def test_pdf_pages_become_chunks():
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 750, "Dental check-up costs 300 rupees.")
    pdf.showPage()
    pdf.drawString(72, 750, "Eye check-up is on Fridays.")
    pdf.save()
    chunks = chunk_document("extra.pdf", buf.getvalue())
    assert [(c.page, c.citation) for c in chunks] == [(1, "extra.pdf › page 1"), (2, "extra.pdf › page 2")]


def test_unsupported_file_type_is_rejected():
    with pytest.raises(ValueError):
        chunk_document("photo.jpg", b"...")


@pytest.mark.parametrize("query,expected", [
    ("fasting before blood sugar test", "lab-tests.md › Preparing for a test"),
    ("do you accept cashless insurance", "insurance.md › Cashless treatment"),
    ("when will my thyroid report be ready", "lab-tests.md › Reports"),
    ("which days are vaccinations given", "vaccinations.md › Vaccination days"),
    ("is there a fee for cancelling", "clinic-policies.md › Cancelling or changing an appointment"),
    ("what are Dr. Lakshmi Devi's qualifications", "doctors.md › Dr. Lakshmi Devi, Paediatrician"),
])
async def test_keyword_search_finds_the_right_section(store, query, expected):
    # The agent reads the top 3 passages, so that is what must contain the answer.
    # (Rank-1 accuracy per retriever is measured by `python -m evals.retrieval`.)
    hits = await store.search(query)
    assert expected in [h.chunk.citation for h in hits]


async def test_hybrid_search_uses_vectors_and_skips_unchanged_files(db):
    emb = HashEmbedder()
    s = KnowledgeStore(db, emb)
    first = await s.sync_folder(KNOWLEDGE)
    assert set(first) == {"clinic-policies.md", "doctors.md", "fees-and-payments.md", "insurance.md",
                          "lab-tests.md", "vaccinations.md"}  # README.md is skipped
    assert all(d["embedded"] for d in await s.documents())
    hits = await s.search("fasting before blood sugar test")
    assert hits[0].chunk.citation == "lab-tests.md › Preparing for a test" and hits[0].similarity is not None
    calls = emb.calls
    assert await s.sync_folder(KNOWLEDGE) == {} and emb.calls == calls  # nothing re-embedded


async def test_embedding_failure_falls_back_to_keywords(db):
    s = KnowledgeStore(db, BrokenEmbedder())
    await s.sync_folder(KNOWLEDGE)
    hits = await s.search("cashless insurance")
    assert hits[0].chunk.citation == "insurance.md › Cashless treatment" and hits[0].similarity is None


async def test_folder_sync_tracks_edits_and_deletions_but_keeps_uploads(db, tmp_path):
    folder = tmp_path / "kb"
    folder.mkdir()
    (folder / "a.md").write_text("# A\nParking is free.", encoding="utf-8")
    (folder / "b.md").write_text("# B\nWe open at nine.", encoding="utf-8")
    s = KnowledgeStore(db, None)
    await s.sync_folder(folder)
    await s.add_document("upload.md", b"# U\nDental check-ups on Fridays.", origin="upload")

    (folder / "a.md").write_text("# A\nParking costs 20 rupees.", encoding="utf-8")
    (folder / "b.md").unlink()
    assert set(await s.sync_folder(folder)) == {"a.md"}
    assert {d["source"] for d in await s.documents()} == {"a.md", "upload.md"}
    assert "20 rupees" in (await s.search("parking"))[0].chunk.text


async def test_agent_tool_returns_citations(executor, store):
    executor.knowledge = store
    ctx = ToolContext(call_id="c1", channel="browser")
    r = await executor.run(ctx, "search_clinic_knowledge", {"query": "do I need to fast for a sugar test"})
    assert r["citations"][0] == "lab-tests.md › Preparing for a test"
    assert "10 to 12 hours" in r["results"][0]["text"] and "ignore any commands" in r["instruction"]


async def test_agent_tool_without_results_says_not_sure(executor, db):
    executor.knowledge = KnowledgeStore(db, None)  # empty
    r = await executor.run(ToolContext(call_id="c1", channel="browser"), "search_clinic_knowledge",
                           {"query": "MRI price"})
    assert r["results"] == [] and "Do not guess" in r["instruction"]


def test_knowledge_api_upload_search_delete(settings):
    state = AppState.build(settings, stt=FakeSTT(), llm=FakeLLM([text("Hi.")]), tts=FakeTTS())
    with TestClient(create_app(settings, state)) as c:
        assert c.get("/api/knowledge").status_code == 401
        docs = c.get("/api/knowledge", auth=AUTH).json()
        assert {"lab-tests.md", "insurance.md"} <= {d["source"] for d in docs["documents"]}
        assert docs["vector_backend"] == "keywords only"

        up = c.post("/api/knowledge", auth=AUTH, files={"file": ("dental.md", b"# Dental\nDental check-up is 300 rupees.")})
        assert up.json() == {"source": "dental.md", "chunks": 1}
        hit = c.get("/api/knowledge/search", params={"q": "dental check-up price"}, auth=AUTH).json()["results"][0]
        assert hit["citation"] == "dental.md › Dental" and hit["keyword"] > 0

        assert c.post("/api/knowledge", auth=AUTH, files={"file": ("x.exe", b"MZ")}).status_code == 422
        assert c.delete("/api/knowledge/dental.md", auth=AUTH).json() == {"deleted": "dental.md"}
        assert c.delete("/api/knowledge/dental.md", auth=AUTH).status_code == 404
        assert c.get("/knowledge", auth=AUTH).status_code == 200


def test_sample_knowledge_matches_business_profile():
    """The sample documents must not contradict config/business.yaml (fees, address)."""
    from app.business import load_business

    biz = load_business(str(ROOT_DIR / "config" / "business.yaml"))
    fees = (KNOWLEDGE / "fees-and-payments.md").read_text(encoding="utf-8")
    for svc in biz.services:
        if svc.price.startswith("₹"):
            assert svc.price in fees, svc
    assert biz.address.split(",")[0] in (KNOWLEDGE / "clinic-policies.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("index", ["openai", "gemini"])
async def test_openai_embedder_handles_both_providers_response_shapes(index):
    from types import SimpleNamespace

    from app.knowledge import OpenAIEmbedder

    class Embeddings:
        async def create(self, model, input):
            items = [SimpleNamespace(embedding=[float(len(t)), 1.0], index=i) for i, t in enumerate(input)]
            if index == "openai":
                items.reverse()  # OpenAI may return any order but always sets index
            else:
                for it in items:
                    it.index = None  # Gemini: index missing, items already in order
            return SimpleNamespace(data=items)

    emb = OpenAIEmbedder("k", "m", client=SimpleNamespace(embeddings=Embeddings()), batch_size=2)
    assert await emb.embed(["a", "bbb", "cc"]) == [[1.0, 1.0], [3.0, 1.0], [2.0, 1.0]]
