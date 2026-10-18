"""Clinic knowledge base: indexing and hybrid retrieval (BM25 keywords + vector similarity).

* Documents come from the `knowledge/` folder (synced at startup) or dashboard uploads.
* Each document is split into heading-sized chunks and embedded once; unchanged files are skipped.
* Search = Reciprocal Rank Fusion of BM25 and cosine similarity. On Postgres the vector part runs in
  SQL with pgvector; elsewhere it runs in NumPy. Without an embedding model it degrades to BM25 only,
  so the agent can always answer from the documents.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sqlalchemy import delete, func, select, text

from ..db import Database, KnowledgeChunk
from .documents import Chunk, chunk_document, content_hash, folder_documents
from .embeddings import Embedder
from .ranking import BM25, cosine, rrf

log = logging.getLogger(__name__)
CANDIDATES = 20  # how many results each retriever contributes to the fusion


@dataclass
class Hit:
    chunk: Chunk
    score: float  # fused RRF score
    keyword: float  # BM25 score (0 = no shared words)
    similarity: float | None  # cosine similarity, None if no embeddings

    def as_dict(self) -> dict:
        return {"source": self.chunk.source, "section": self.chunk.section, "page": self.chunk.page,
                "citation": self.chunk.citation, "text": self.chunk.text, "score": round(self.score, 4),
                "keyword": round(self.keyword, 3),
                "similarity": None if self.similarity is None else round(self.similarity, 3)}


class KnowledgeStore:
    def __init__(self, db: Database, embedder: Embedder | None = None, top_k: int = 3):
        self.db = db
        self.embedder = embedder
        self.top_k = top_k
        self._ids: list[str] = []
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None  # rows aligned with _chunks; NaN rows = not embedded
        self._bm25 = BM25([])

    # ------------------------------------------------------------------ indexing
    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.embedder or not texts:
            return None
        try:
            return await self.embedder.embed(texts)
        except Exception as exc:  # quota, wrong model name, offline...: keyword search still works
            log.warning("Embedding failed (%s: %s); using keyword search only", type(exc).__name__, str(exc)[:200])
            return None

    async def add_document(self, source: str, data: bytes, origin: str = "upload") -> int:
        """(Re)index one document. Returns the number of chunks."""
        chunks = chunk_document(source, data)
        if not chunks:
            raise ValueError("no text found in the document")
        vectors = await self._embed([c.search_text for c in chunks])
        model = self.embedder.model if (self.embedder and vectors) else None
        doc_hash = content_hash(data)
        async with self.db.session() as s:
            await s.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source == source))
            s.add_all(KnowledgeChunk(source=source, origin=origin, doc_hash=doc_hash, position=i,
                                     section=c.section, page=c.page, text=c.text,
                                     embedding=vectors[i] if vectors else None, embedding_model=model)
                      for i, c in enumerate(chunks))
            await s.commit()
        await self.load()
        log.info("Indexed %s: %d chunks (%s)", source, len(chunks), model or "keywords only")
        return len(chunks)

    async def delete_document(self, source: str) -> bool:
        async with self.db.session() as s:
            result = await s.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source == source))
            await s.commit()
        await self.load()
        return bool(result.rowcount)

    async def sync_folder(self, folder: Path) -> dict[str, int]:
        """Index new/changed files, re-embed if the embedding model changed, drop deleted files."""
        files = folder_documents(folder)
        async with self.db.session() as s:
            rows = (await s.execute(select(KnowledgeChunk.source, KnowledgeChunk.origin, KnowledgeChunk.doc_hash,
                                           KnowledgeChunk.embedding_model).distinct())).all()
        known = {r.source: r for r in rows}
        want_model = self.embedder.model if self.embedder else None
        changed = {}
        for name, data in files.items():
            row = known.get(name)
            stale_vectors = want_model is not None and (row is None or row.embedding_model != want_model)
            if row is None or row.doc_hash != content_hash(data) or stale_vectors:
                changed[name] = await self.add_document(name, data, origin="folder")
        for name, row in known.items():
            if row.origin == "folder" and name not in files:
                await self.delete_document(name)
        await self.load()
        return changed

    async def load(self) -> None:
        async with self.db.session() as s:
            rows = (await s.execute(select(KnowledgeChunk).order_by(KnowledgeChunk.source,
                                                                    KnowledgeChunk.position))).scalars().all()
        self._ids = [r.id for r in rows]
        self._chunks = [Chunk(r.source, r.section, r.text, r.page) for r in rows]
        self._bm25 = BM25([c.search_text for c in self._chunks])
        model = self.embedder.model if self.embedder else None
        dims = {len(r.embedding) for r in rows if r.embedding and r.embedding_model == model}
        if model and len(dims) == 1:
            d = dims.pop()
            self._vectors = np.array([r.embedding if (r.embedding and r.embedding_model == model) else [np.nan] * d
                                      for r in rows], dtype=np.float32)
        else:
            self._vectors = None

    async def documents(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(
                select(KnowledgeChunk.source, KnowledgeChunk.origin, func.count(KnowledgeChunk.id),
                       func.count(KnowledgeChunk.embedding_model), func.max(KnowledgeChunk.created_at))
                .group_by(KnowledgeChunk.source, KnowledgeChunk.origin).order_by(KnowledgeChunk.source))).all()
        return [{"source": src, "origin": origin, "chunks": n, "embedded": emb == n and n > 0,
                 "updated": updated.isoformat() if updated else None} for src, origin, n, emb, updated in rows]

    # ------------------------------------------------------------------ retrieval
    async def search(self, query: str, k: int | None = None, mode: str = "hybrid") -> list[Hit]:
        """mode: "hybrid" (default), "keyword" (BM25 only) or "vector" (embeddings only, for evals)."""
        k = k or self.top_k
        if not self._chunks or not query.strip():
            return []
        keyword = self._bm25.scores(query)
        keyword_rank = [i for i in np.argsort(keyword)[::-1][:CANDIDATES] if keyword[i] > 0]
        if mode == "vector":
            keyword_rank = []

        similarity: dict[int, float] = {}
        if self._vectors is not None and mode != "keyword":
            qv = await self._embed([query])
            if qv:
                similarity = await self._vector_scores(np.array(qv[0], dtype=np.float32))
        vector_rank = sorted(similarity, key=similarity.get, reverse=True)[:CANDIDATES]

        fused = rrf([keyword_rank, vector_rank])
        best = sorted(fused, key=fused.get, reverse=True)[:k]
        return [Hit(self._chunks[i], fused[i], float(keyword[i]), similarity.get(i)) for i in best]

    async def _vector_scores(self, q: np.ndarray) -> dict[int, float]:
        if self.db.pgvector and self.embedder:
            # Exact cosine search inside Postgres. (A clinic has hundreds of chunks; add an HNSW index
            # on a vector(n) column only if this grows to many thousands.)
            sql = text("""SELECT id, 1 - ((embedding::text)::vector <=> CAST(:q AS vector)) AS sim
                          FROM knowledge_chunks WHERE embedding IS NOT NULL AND embedding_model = :m
                          ORDER BY (embedding::text)::vector <=> CAST(:q AS vector) LIMIT :n""")
            async with self.db.session() as s:
                rows = (await s.execute(sql, {"q": "[" + ",".join(map(str, q.tolist())) + "]",
                                              "m": self.embedder.model, "n": CANDIDATES})).all()
            pos = {cid: i for i, cid in enumerate(self._ids)}
            return {pos[r.id]: float(r.sim) for r in rows if r.id in pos}
        valid = ~np.isnan(self._vectors).any(axis=1)
        sims = cosine(np.nan_to_num(self._vectors), q)
        return {int(i): float(sims[i]) for i in np.flatnonzero(valid)}
