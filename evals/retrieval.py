"""Retrieval eval for the clinic knowledge base: hit@1, hit@3 and MRR per search mode and language.

    python -m evals.retrieval            # keyword vs vector vs hybrid (vector needs an embedding model)
    python -m evals.retrieval --keyword-only

Writes evals/retrieval_results.json and prints a Markdown table for the README.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

from app.config import get_settings
from app.db import Database
from app.knowledge import KnowledgeStore, OpenAIEmbedder

HERE = Path(__file__).parent
GROUPS = {"en": "English", "paraphrase": "English, paraphrased", "hi": "Hindi", "te": "Telugu"}


async def evaluate(store: KnowledgeStore, cases: list[dict], mode: str) -> dict:
    per_group: dict[str, list[int | None]] = defaultdict(list)
    misses = []
    for c in cases:
        hits = await store.search(c["q"], k=5, mode=mode)
        cites = [h.chunk.citation for h in hits]
        rank = next((i + 1 for i, cite in enumerate(cites) if cite in c["expect"]), None)
        per_group[c["lang"]].append(rank)
        per_group["all"].append(rank)
        if rank != 1:
            misses.append({"q": c["q"], "expected": c["expect"][0], "got": cites[:3], "rank": rank})

    def metrics(ranks: list[int | None]) -> dict:
        n = len(ranks)
        return {"n": n, "hit@1": round(sum(r == 1 for r in ranks) / n, 3),
                "hit@3": round(sum(r is not None and r <= 3 for r in ranks) / n, 3),
                "mrr": round(sum(1 / r for r in ranks if r) / n, 3)}

    return {"groups": {g: metrics(r) for g, r in per_group.items()}, "misses": misses}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword-only", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    cases = yaml.safe_load(open(HERE / "retrieval.yaml", encoding="utf-8"))
    model = "" if args.keyword_only else s.resolved_embedding_model
    embedder = OpenAIEmbedder(s.openai_api_key, model, base_url=s.openai_base_url or None) if model else None

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Database(f"sqlite+aiosqlite:///{tmp}/kb.db")
        await db.create_all()
        store = KnowledgeStore(db, embedder)
        try:
            await store.sync_folder(Path(s.knowledge_dir))
            vectors = store._vectors is not None
            if embedder and not vectors:
                print(f"Embedding model {model!r} failed (see warning above): showing keyword search only.")
            modes = ["keyword", "vector", "hybrid"] if vectors else ["keyword"]
            results = {m: await evaluate(store, cases, m) for m in modes}
        finally:
            await db.dispose()

    print(f"\nRetrieval over {len(cases)} questions · embedding model: {model if vectors else 'none'}\n")
    print("| Search | " + " | ".join(f"{GROUPS[g]} hit@1" for g in GROUPS) + " | All hit@1 | All hit@3 | MRR |")
    print("|---|" + "---|" * (len(GROUPS) + 3))
    for m, r in results.items():
        g = r["groups"]
        print(f"| {m} | " + " | ".join(f"{g[k]['hit@1']:.0%}" for k in GROUPS if k in g)
              + f" | {g['all']['hit@1']:.0%} | {g['all']['hit@3']:.0%} | {g['all']['mrr']:.2f} |")
    best = results.get("hybrid") or results["keyword"]
    if best["misses"]:
        print("\nNot ranked first (" + ("hybrid" if "hybrid" in results else "keyword") + "):")
        for miss in best["misses"]:
            print(f"  rank={miss['rank']}  {miss['q']}\n      expected {miss['expected']}, got {miss['got']}")
    (HERE / "retrieval_results.json").write_text(json.dumps({"model": model if vectors else None, **results},
                                                            indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
