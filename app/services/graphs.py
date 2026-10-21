"""Collect OLT-level graph data (not per ONU) from standard/validated sources:
 - if_in / if_out : bit/s per interface (PON & uplink) from IF-MIB ifHCIn/OutOctets
 - pon_rx         : average Rx of online ONUs per PON (from ONU database data)
 - onu_status     : ONU count per status
 - up_rx/up_tx/up_temp : uplink optical module Rx/Tx/temperature (`show interface optical-module-info`)
OLT temperature/fan/CPU/memory require ZTE-specific OIDs/commands that are not available yet."""
import logging
import re
from datetime import timedelta

from sqlalchemy import delete, func, select

from app.config import settings
from app.db import SessionLocal
from app.drivers import zte
from app.drivers.snmp import SnmpError
from app.models import GraphSample, OLT, ONU, utcnow
from app.services import sync

log = logging.getLogger("smartolt.graphs")
IF_KEEP = re.compile(r"^(?:gpon[-_]olt|x?gei|ge|fei|smartgroup)[-_]\S+$", re.I)
_PREV: dict[tuple, tuple] = {}     # (olt_id, ifname) -> (monotonic_ts, in_octets, out_octets)


def is_pon(name: str) -> bool:
    return bool(re.match(r"^gpon[-_]olt", name, re.I))


async def sample_olt(olt_id: int) -> int:
    import time
    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if not olt:
            return 0
        try:
            conn = zte.conn_from_row(olt)
        except ValueError:
            return 0
    now, rows = utcnow(), []
    try:
        table = await zte.snmp_if_table(conn, with_counters=True)
        mono = time.monotonic()
        for name, r in table.items():
            if not IF_KEEP.match(name) or r.get("in") is None or r.get("out") is None:
                continue
            prev = _PREV.get((olt_id, name))
            _PREV[(olt_id, name)] = (mono, r["in"], r["out"])
            if prev and mono > prev[0] and r["in"] >= prev[1] and r["out"] >= prev[2]:
                dt = mono - prev[0]
                rows.append(GraphSample(olt_id=olt_id, metric="if_in", key=name, ts=now,
                                        value=(r["in"] - prev[1]) * 8 / dt))
                rows.append(GraphSample(olt_id=olt_id, metric="if_out", key=name, ts=now,
                                        value=(r["out"] - prev[2]) * 8 / dt))
    except SnmpError as exc:
        log.info("Traffic graph for %s skipped: %s", conn.name, exc)
    try:   # Uplink optical module Rx/Tx/temperature (the command does not support copper ports).
        async with SessionLocal() as s:
            cfg, _ = await sync.load_info(s, olt_id, "uplinks")
        ports = [n for n, c in cfg.items() if c.get("admin") != "Disabled"]
        if ports:
            for name, d in (await zte.read_optical(conn, ports)).items():
                for metric, key in (("up_rx", "rx"), ("up_tx", "tx"), ("up_temp", "temp")):
                    if d[key] is not None:
                        rows.append(GraphSample(olt_id=olt_id, metric=metric, key=name, ts=now, value=d[key]))
    except Exception as exc:
        log.info("Optical graph for %s skipped: %s", conn.name, exc)
    async with SessionLocal() as s:
        for slot, pon, avg in (await s.execute(
                select(ONU.slot, ONU.pon, func.avg(ONU.rx_power))
                .where(ONU.olt_id == olt_id, ONU.status == "online", ONU.rx_power.is_not(None))
                .group_by(ONU.slot, ONU.pon))).all():
            rows.append(GraphSample(olt_id=olt_id, metric="pon_rx", key=f"{slot}/{pon}", ts=now, value=float(avg)))
        counts = (await sync.counts_for(s, olt_id)).get(olt_id)
        for st in ("online", "offline", "los", "power_fail"):
            if counts:
                rows.append(GraphSample(olt_id=olt_id, metric="onu_status", key=st, ts=now, value=counts.get(st, 0)))
        s.add_all(rows)
        await s.commit()
    return len(rows)


async def cleanup(days: int | None = None) -> int:
    days = days or int(__import__("os").getenv("GRAPH_RETENTION_DAYS", "14"))
    async with SessionLocal() as s:
        res = await s.execute(delete(GraphSample).where(GraphSample.ts < utcnow() - timedelta(days=days)))
        await s.commit()
        return res.rowcount or 0
