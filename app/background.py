"""Fire-and-forget work (SMS, call summaries) that must not be lost or cut off half-way.

``spawn`` keeps a reference to each task (asyncio only keeps weak references, so an unreferenced task can be
garbage-collected mid-run) and ``drain`` lets them finish at shutdown, before the database is closed. A task
cancelled in the middle of a database write leaves a broken connection behind (seen as "database is locked"
or a hang at shutdown).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_done)
    return task


def _done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("Background task %s failed: %r", task.get_name(), task.exception())


async def drain(timeout: float = 5.0) -> None:
    """Wait for this event loop's background tasks (at shutdown); cancel whatever is still running after that."""
    loop = asyncio.get_running_loop()
    pending = [t for t in _tasks if not t.done() and t.get_loop() is loop]
    if not pending:
        return
    _, still = await asyncio.wait(pending, timeout=timeout)
    for t in still:
        t.cancel()
    if still:
        await asyncio.gather(*still, return_exceptions=True)
