"""Dormant background queue.

The original FIFO queue served the Playwright portal-submission step.
That step is currently parked behind the disabled "Submit to TMS Portal"
button, so the queue is intentionally not started. When we re-enable
portal submission, we'll switch from Job to LR/Invoice as the unit of
work and revive this module.
"""
from __future__ import annotations

import asyncio
from typing import Optional


class JobQueue:
    """Stub kept so future code can `from app.services.queue import JobQueue`."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    @property
    def size(self) -> int:
        return self._queue.qsize()


_queue: Optional[JobQueue] = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue
