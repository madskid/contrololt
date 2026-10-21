"""Event bus for pushing realtime updates to the browser.

- Without REDIS_URL: in-memory (web and worker in one process).
- With REDIS_URL: Redis pub/sub connects workers and multiple web instances.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from app.config import settings

log = logging.getLogger("smartolt.bus")
CHANNEL = "smartolt:events"


class MemoryBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    async def publish(self, event: dict) -> None:
        for q in list(self._subs):
            if q.full():  # Slow client: discard the oldest event.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        try:
            yield q
        finally:
            self._subs.discard(q)


class RedisBus:
    def __init__(self, client) -> None:
        self._r = client

    async def publish(self, event: dict) -> None:
        try:
            await self._r.publish(CHANNEL, json.dumps(event))
        except Exception as exc:  # realtime bersifat best-effort
            log.warning("Redis publish failed: %s", exc)

    @asynccontextmanager
    async def subscribe(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        ps = self._r.pubsub()
        await ps.subscribe(CHANNEL)

        async def pump():
            async for msg in ps.listen():
                if msg.get("type") == "message":
                    try:
                        q.put_nowait(json.loads(msg["data"]))
                    except asyncio.QueueFull:
                        pass

        task = asyncio.create_task(pump())
        try:
            yield q
        finally:
            task.cancel()
            try:
                await ps.unsubscribe(CHANNEL)
                await (ps.aclose() if hasattr(ps, "aclose") else ps.close())
            except Exception:
                pass


def make_bus(redis_client=None):
    if redis_client is not None:
        return RedisBus(redis_client)
    if settings.redis_url:
        import redis.asyncio as aioredis
        return RedisBus(aioredis.from_url(settings.redis_url, decode_responses=True))
    return MemoryBus()


bus = make_bus()
