import unittest
from unittest.mock import AsyncMock, patch

import app.models
from app.db import Base
from app.models import OLT, ONU, OnuMacAddress
from app.services import sync
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class HiosoLearnedMacSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_learned_client_mac_is_stored_with_the_onu_identity(self):
        async with self.sessions() as session:
            olt = OLT(name="HIOSO", ip="192.0.2.3", vendor="hioso")
            session.add(olt)
            await session.commit()
            olt_id = olt.id

        data = {(1, 2): {"mac": "AABBCCDDEEFF", "state": "1", "rx": "-20", "name": ""}}
        learned = {(1, 2): ["001122334455"]}
        with (
            patch.object(sync, "SessionLocal", self.sessions),
            patch.object(sync.hioso, "system_info", new=AsyncMock(return_value={
                "sys_descr": "HIOSO", "uptime_s": 1, "version": "1", "firmware": "HA7308C",
            })),
            patch.object(sync.hioso, "read_onus", new=AsyncMock(return_value=data)),
            patch.object(sync.hioso, "read_onu_macs", new=AsyncMock(return_value=learned)),
            patch.object(sync.bus, "publish", new=AsyncMock()),
        ):
            await sync._hioso_full_sync(olt_id, object(), olt)

        async with self.sessions() as session:
            onu = (await session.scalars(select(ONU).where(ONU.olt_id == olt_id))).one()
            macs = set((await session.scalars(
                select(OnuMacAddress.mac_compact).where(OnuMacAddress.onu_id == onu.id)
            )).all())
            self.assertEqual(macs, {"AABBCCDDEEFF", "001122334455"})


if __name__ == "__main__":
    unittest.main()
