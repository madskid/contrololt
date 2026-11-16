import unittest
from unittest.mock import AsyncMock, patch

import app.models
from app.db import Base
from app.models import OLT, ONU, OnuMacAddress, OnuWanIp
from app.services import sync
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class CdataDuplicateMacSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_mac_already_owned_by_another_olt_is_hidden_from_cdata(self):
        duplicate_mac = "98C7A421723C"
        async with self.sessions() as session:
            owner_olt = OLT(name="OLT OWNER", ip="192.0.2.1", vendor="cdata")
            epon_olt = OLT(name="EPON", ip="192.0.2.2", vendor="cdata")
            session.add_all((owner_olt, epon_olt))
            await session.flush()

            owner = ONU(olt_id=owner_olt.id, shelf=0, slot=1, pon=1, onu_number=1, sn=duplicate_mac)
            duplicate = ONU(olt_id=epon_olt.id, shelf=0, slot=1, pon=1, onu_number=1, sn=duplicate_mac)
            session.add_all((owner, duplicate))
            await session.flush()
            session.add_all((
                OnuMacAddress(onu_id=owner.id, mac=duplicate_mac, mac_compact=duplicate_mac),
                OnuMacAddress(onu_id=duplicate.id, mac=duplicate_mac, mac_compact=duplicate_mac),
                OnuWanIp(onu_id=duplicate.id, mac=duplicate_mac, mac_compact=duplicate_mac,
                         router="ROUTER", username="wrong-user", ip="10.0.0.2"),
            ))
            await session.commit()
            epon_olt_id, duplicate_id = epon_olt.id, duplicate.id

        data = {
            (1, 1, 1): {"mac": duplicate_mac, "state": "1", "rx": "-20", "name": ""},
            (1, 1, 2): {"mac": duplicate_mac, "state": "1", "rx": "-20", "name": ""},
            (1, 2, 1): {"mac": "AABBCCDDEEFF", "state": "1", "rx": "-20", "name": ""},
        }
        with (
            patch.object(sync, "SessionLocal", self.sessions),
            patch.object(sync.cdata, "system_info", new=AsyncMock(return_value={
                "sys_descr": "C-Data", "uptime_s": 1,
            })),
            patch.object(sync.cdata, "read_onus", new=AsyncMock(return_value=data)),
            patch.object(sync.cdata, "read_onu_macs", new=AsyncMock(return_value={})),
            patch.object(sync.bus, "publish", new=AsyncMock()),
        ):
            await sync._cdata_full_sync(epon_olt_id, object())

        async with self.sessions() as session:
            duplicate = await session.get(ONU, duplicate_id)
            self.assertTrue(duplicate.hidden_duplicate)
            visible = (await session.scalars(
                select(ONU).where(ONU.olt_id == epon_olt_id, ONU.hidden_duplicate.is_(False))
            )).all()
            self.assertEqual([(onu.pon, onu.onu_number) for onu in visible], [(2, 1)])
            self.assertEqual((await session.scalars(
                select(OnuMacAddress).where(OnuMacAddress.onu_id == duplicate_id)
            )).all(), [])
            self.assertEqual((await session.scalars(
                select(OnuWanIp).where(OnuWanIp.onu_id == duplicate_id)
            )).all(), [])

    async def test_learned_client_mac_is_stored_with_the_onu_identity(self):
        async with self.sessions() as session:
            olt = OLT(name="EPON", ip="192.0.2.2", vendor="cdata")
            session.add(olt)
            await session.commit()
            olt_id = olt.id

        data = {(1, 1, 1): {"mac": "AABBCCDDEEFF", "state": "1", "rx": "-20", "name": ""}}
        learned = {(1, 1, 1): ["001122334455"]}
        with (
            patch.object(sync, "SessionLocal", self.sessions),
            patch.object(sync.cdata, "system_info", new=AsyncMock(return_value={
                "sys_descr": "C-Data", "uptime_s": 1,
            })),
            patch.object(sync.cdata, "read_onus", new=AsyncMock(return_value=data)),
            patch.object(sync.cdata, "read_onu_macs", new=AsyncMock(return_value=learned)),
            patch.object(sync.bus, "publish", new=AsyncMock()),
        ):
            await sync._cdata_full_sync(olt_id, object())

        async with self.sessions() as session:
            onu = (await session.scalars(select(ONU).where(ONU.olt_id == olt_id))).one()
            macs = set((await session.scalars(
                select(OnuMacAddress.mac_compact).where(OnuMacAddress.onu_id == onu.id)
            )).all())
            self.assertEqual(macs, {"AABBCCDDEEFF", "001122334455"})

        with (
            patch.object(sync, "SessionLocal", self.sessions),
            patch.object(sync.cdata, "system_info", new=AsyncMock(return_value={
                "sys_descr": "C-Data", "uptime_s": 1,
            })),
            patch.object(sync.cdata, "read_onus", new=AsyncMock(return_value=data)),
            patch.object(sync.cdata, "read_onu_macs", new=AsyncMock(return_value={})),
            patch.object(sync.bus, "publish", new=AsyncMock()),
        ):
            await sync._cdata_full_sync(olt_id, object())

        async with self.sessions() as session:
            onu = (await session.scalars(select(ONU).where(ONU.olt_id == olt_id))).one()
            macs = set((await session.scalars(
                select(OnuMacAddress.mac_compact).where(OnuMacAddress.onu_id == onu.id)
            )).all())
            self.assertEqual(macs, {"AABBCCDDEEFF"})


if __name__ == "__main__":
    unittest.main()
