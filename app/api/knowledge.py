"""Knowledge base admin API: list, upload, delete and test-search the clinic's documents."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from ..security import require_admin

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"], dependencies=[Depends(require_admin)])
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _store(request: Request):
    return request.app.state.container.knowledge


@router.get("")
async def list_documents(request: Request) -> dict:
    store = _store(request)
    return {"documents": await store.documents(),
            "embedding_model": store.embedder.model if store.embedder else None,
            "vector_backend": "pgvector" if store.db.pgvector else ("numpy" if store.embedder else "keywords only")}


@router.post("")
async def upload_document(request: Request, file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "").name
    if not name:
        raise HTTPException(422, "file name missing")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file is larger than 5 MB")
    try:
        chunks = await _store(request).add_document(name, data, origin="upload")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"source": name, "chunks": chunks}


@router.delete("/{source}")
async def delete_document(request: Request, source: str) -> dict:
    if not await _store(request).delete_document(source):
        raise HTTPException(404, "document not found")
    return {"deleted": source}


@router.get("/search")
async def search(request: Request, q: str, k: int = 5) -> dict:
    hits = await _store(request).search(q, k=min(max(k, 1), 10))
    return {"query": q, "results": [h.as_dict() for h in hits]}
