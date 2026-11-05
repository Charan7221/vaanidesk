"""Back up and restore the VaaniDesk database. Works for SQLite and PostgreSQL (no pg_dump needed).

    python -m scripts.backup export backups/vaanidesk-2026-11-05.json.gz   # uses DATABASE_URL from .env
    python -m scripts.backup verify backups/vaanidesk-2026-11-05.json.gz   # restore drill into a temp database
    python -m scripts.backup restore backups/vaanidesk-2026-11-05.json.gz  # into an EMPTY database

Run `export` daily (cron / Render cron job) and copy the file off the server. Run `verify` regularly: a
backup that has never been restored is not a backup.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, func, insert, select

from app.config import get_settings
from app.db import Base, Database

FORMAT_VERSION = 1


def _encode(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _decode(column, value):
    if value is not None and isinstance(column.type, DateTime) and isinstance(value, str):
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return value


async def export(db: Database, path: Path) -> dict[str, int]:
    await db.create_all()
    data: dict = {"format": FORMAT_VERSION, "created": datetime.now(UTC).isoformat(), "tables": {}}
    async with db.engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = (await conn.execute(select(table))).mappings().all()
            data["tables"][table.name] = [{k: _encode(v) for k, v in r.items()} for r in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return {name: len(rows) for name, rows in data["tables"].items()}


async def counts(db: Database) -> dict[str, int]:
    async with db.engine.connect() as conn:
        return {t.name: (await conn.execute(select(func.count()).select_from(t))).scalar_one()
                for t in Base.metadata.sorted_tables}


async def restore(db: Database, path: Path, allow_nonempty: bool = False) -> dict[str, int]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != FORMAT_VERSION:
        raise SystemExit(f"Unknown backup format: {data.get('format')}")
    await db.create_all()
    if not allow_nonempty and any((await counts(db)).values()):
        raise SystemExit("The target database is not empty. Restore into a fresh database (or pass --force).")
    async with db.engine.begin() as conn:
        for table in Base.metadata.sorted_tables:  # parents before children (foreign keys)
            rows = data["tables"].get(table.name, [])
            cols = {c.name: c for c in table.columns}
            clean = [{k: _decode(cols[k], v) for k, v in r.items() if k in cols} for r in rows]
            if clean:
                await conn.execute(insert(table), clean)
    return {name: len(rows) for name, rows in data["tables"].items()}


async def verify(path: Path) -> dict[str, int]:
    """Restore drill: load the backup into a throwaway SQLite database and check every row arrived."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'restore-test.db'}")
        try:
            expected = await restore(db, path)
            got = await counts(db)
        finally:
            await db.dispose()
    bad = {t: (n, got.get(t)) for t, n in expected.items() if got.get(t) != n}
    if bad:
        raise SystemExit(f"Restore drill FAILED, row counts differ: {bad}")
    return got


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["export", "restore", "verify"])
    ap.add_argument("file", type=Path)
    ap.add_argument("--database-url", default=None, help="defaults to DATABASE_URL from .env")
    ap.add_argument("--force", action="store_true", help="restore into a database that already has data")
    args = ap.parse_args()
    if args.action == "verify":
        print("Restore drill OK:", await verify(args.file))
        return
    db = Database(args.database_url or get_settings().database_url)
    try:
        if args.action == "export":
            print(f"Backed up to {args.file}:", await export(db, args.file))
        else:
            print("Restored:", await restore(db, args.file, allow_nonempty=args.force))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
