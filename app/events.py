"""In-process pub/sub for Server-Sent Events.

Topics: "desk" (support desk sees new/updated tickets) and "conv:<id>" (a customer's chat sees agent follow-ups).
publish() is thread-safe: tools run in FastAPI's threadpool, subscribers live on the event loop.
Production: swap the body of publish()/subscribe() for Redis pub/sub or Postgres LISTEN/NOTIFY so
events cross processes; callers don't change.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self._lock = threading.Lock()

    @asynccontextmanager
    async def subscribe(self, topic: str) -> AsyncIterator[asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        entry = (asyncio.get_running_loop(), q)
        with self._lock:
            self._subs.setdefault(topic, set()).add(entry)
        try:
            yield q
        finally:
            with self._lock:
                self._subs.get(topic, set()).discard(entry)

    def publish(self, topic: str, event: dict) -> int:
        with self._lock:
            subs = list(self._subs.get(topic, ()))
        for loop, q in subs:
            loop.call_soon_threadsafe(self._put, q, event)
        return len(subs)

    @staticmethod
    def _put(q: asyncio.Queue, event: dict) -> None:
        if not q.full():
            q.put_nowait(event)


bus = EventBus()


def sse(event: dict) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"
