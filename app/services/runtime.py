"""Supervisor: one loop per OLT (a slow OLT does not block others) plus daily save scheduler."""
import asyncio
import logging
import random
from datetime import datetime

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.drivers import zte
from app.models import OLT, utcnow
from app.services import graphs, sync

log = logging.getLogger("smartolt.runtime")


class Supervisor:
    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task] = {}

    async def reconcile(self) -> None:
        async with SessionLocal() as s:
            ids = set(await s.scalars(select(OLT.id)))
        for i in ids - self.tasks.keys():
            self.tasks[i] = asyncio.create_task(self._olt_loop(i), name=f"olt-{i}")
        for i in set(self.tasks) - ids:
            self.tasks.pop(i).cancel()
            await zte.links.drop(i)
        for i, t in list(self.tasks.items()):  # loop yang mati tak terduga -> hidupkan lagi
            if t.done() and i in ids:
                self.tasks[i] = asyncio.create_task(self._olt_loop(i), name=f"olt-{i}")

    @staticmethod
    async def _flags(olt_id: int):
        async with SessionLocal() as s:
            return (await s.execute(select(OLT.sync_requested, OLT.last_full_sync_at)
                                    .where(OLT.id == olt_id))).first()

    async def _olt_loop(self, olt_id: int) -> None:
        tick = 0
        await asyncio.sleep(random.uniform(0, min(10, settings.poll_interval)))  # sebar beban awal
        while True:
            row = await self._flags(olt_id)
            if row is None:
                return  # The OLT has been deleted.
            try:
                due = (row.sync_requested or row.last_full_sync_at is None or
                       (utcnow() - row.last_full_sync_at).total_seconds() > settings.full_sync_hours * 3600)
                if due:
                    await asyncio.wait_for(sync.run_full_sync(olt_id), 1500)
                else:
                    await asyncio.wait_for(sync.poll_once(olt_id, tick), 400)
                    tick += 1
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                await sync._mark(olt_id, "offline", "Timeout saat sync/poll OLT")
            except Exception:
                log.exception("Loop OLT %s error", olt_id)
            waited = 0
            while waited < settings.poll_interval:  # tidur, tapi cepat bangun kalau diminta sync
                await asyncio.sleep(3)
                waited += 3
                flags = await self._flags(olt_id)
                if flags is None:
                    return
                if flags.sync_requested:
                    break


async def autosave_loop() -> None:
    while True:
        try:
            now_local = datetime.now(sync._tz())
            async with SessionLocal() as s:
                rows = (await s.execute(select(OLT.id, OLT.last_save_at, OLT.last_save_attempt_at))).all()
            due = [r.id for r in rows if sync.save_due(r.last_save_at, r.last_save_attempt_at, now_local)]
            if due:
                log.info("Auto-saving configuration for %d OLTs", len(due))
                await asyncio.gather(*(sync.do_save(i) for i in due), return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autosave_loop error")
        await asyncio.sleep(30)


async def graph_loop() -> None:
    n = 0
    while True:
        try:
            async with SessionLocal() as s:
                ids = list(await s.scalars(select(OLT.id)))
            await asyncio.gather(*(graphs.sample_olt(i) for i in ids), return_exceptions=True)
            n += 1
            if n % 12 == 1:
                await graphs.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("graph_loop error")
        await asyncio.sleep(settings.graph_interval)


async def run_forever() -> None:
    sup = Supervisor()

    async def reconcile_loop():
        while True:
            try:
                await sup.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile error")
            await asyncio.sleep(10)

    await asyncio.gather(reconcile_loop(), autosave_loop(), graph_loop(), zte.links.janitor())
