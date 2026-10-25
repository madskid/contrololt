"""Master data, filtered ONU list, OLT information (cards/PON/uplink/VLAN), unconfigured ONUs, and graphs."""
import asyncio
import csv
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api import _get_olt, _guard, onu_dict
from app.config import settings
from app.db import get_session
from app.drivers import zte
from app.drivers.snmp import SnmpError
from app.models import GraphSample, OLT, ONU, OnuMacAddress, Odb, OnuType, SpeedProfile, Zone, utcnow
from app.security import require_user
from app.services import graphs, sync

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


async def _commit(s: AsyncSession, what: str):
    try:
        await s.commit()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(409, f"{what} already exists")


def _csv(rows: list[list], header: list[str], filename: str) -> StreamingResponse:
    def safe(v):  # cegah CSV/formula injection di Excel
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([safe(x) for x in r])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ───────────────────────── Zones ─────────────────────────
class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class NamesIn(BaseModel):
    names: list[str] = Field(max_length=5000)


class UnconfiguredSerialsIn(BaseModel):
    sns: list[str] = Field(max_length=5000)


@router.get("/zones")
async def zones(s: AsyncSession = Depends(get_session)):
    cnt = dict((await s.execute(select(ONU.zone_id, func.count()).where(ONU.zone_id.is_not(None))
                                .group_by(ONU.zone_id))).all())
    return [{"id": z.id, "name": z.name, "onus": cnt.get(z.id, 0)}
            for z in (await s.scalars(select(Zone).order_by(Zone.name))).all()]


@router.post("/zones", status_code=201)
async def add_zone(b: NameIn, s: AsyncSession = Depends(get_session)):
    z = Zone(name=b.name.strip())
    s.add(z)
    await _commit(s, "Zone")
    return {"id": z.id, "name": z.name, "onus": 0}


@router.put("/zones/{zid}")
async def edit_zone(zid: int, b: NameIn, s: AsyncSession = Depends(get_session)):
    z = await s.get(Zone, zid)
    if not z:
        raise HTTPException(404, "Zone not found")
    z.name = b.name.strip()
    await _commit(s, "Zone")
    return {"id": z.id, "name": z.name}


@router.delete("/zones/{zid}")
async def del_zone(zid: int, s: AsyncSession = Depends(get_session)):
    z = await s.get(Zone, zid)
    if not z:
        raise HTTPException(404, "Zone not found")
    await s.execute(update(ONU).where(ONU.zone_id == zid).values(zone_id=None))
    await s.execute(update(Odb).where(Odb.zone_id == zid).values(zone_id=None))
    await s.delete(z)
    await s.commit()
    return {"ok": True}


@router.post("/zones/delete-unused")
async def del_unused_zones(s: AsyncSession = Depends(get_session)):
    used = select(ONU.zone_id).where(ONU.zone_id.is_not(None)).distinct()
    r = await s.execute(delete(Zone).where(Zone.id.not_in(used)))
    await s.commit()
    return {"deleted": r.rowcount or 0}


@router.post("/zones/import")
async def import_zones(b: NamesIn, s: AsyncSession = Depends(get_session)):
    have = set(await s.scalars(select(Zone.name)))
    new = {n.strip()[:120] for n in b.names if n.strip()} - have
    s.add_all(Zone(name=n) for n in sorted(new))
    await _commit(s, "Zone")
    return {"created": len(new)}


@router.get("/zones/export")
async def export_zones(s: AsyncSession = Depends(get_session)):
    z = await zones(s)
    return _csv([[x["name"], x["onus"]] for x in z], ["Name", "ONUs"], "zones.csv")


# ───────────────────────── ODB / splitter ─────────────────────────
class OdbIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    zone_id: Optional[int] = None


@router.get("/odbs")
async def odbs(zone_id: Optional[int] = None, s: AsyncSession = Depends(get_session)):
    cnt = dict((await s.execute(select(ONU.odb_id, func.count()).where(ONU.odb_id.is_not(None))
                                .group_by(ONU.odb_id))).all())
    zn = dict((await s.execute(select(Zone.id, Zone.name))).all())
    q = select(Odb).order_by(Odb.name)
    if zone_id:
        q = q.where(Odb.zone_id == zone_id)
    return [{"id": o.id, "name": o.name, "zone_id": o.zone_id, "zone": zn.get(o.zone_id), "onus": cnt.get(o.id, 0)}
            for o in (await s.scalars(q)).all()]


async def _check_zone(s, zone_id):
    if zone_id is not None and not await s.get(Zone, zone_id):
        raise HTTPException(422, "Zone does not exist")


@router.post("/odbs", status_code=201)
async def add_odb(b: OdbIn, s: AsyncSession = Depends(get_session)):
    await _check_zone(s, b.zone_id)
    o = Odb(name=b.name.strip(), zone_id=b.zone_id)
    s.add(o)
    await _commit(s, "ODB")
    return {"id": o.id, "name": o.name, "zone_id": o.zone_id, "onus": 0}


@router.put("/odbs/{oid}")
async def edit_odb(oid: int, b: OdbIn, s: AsyncSession = Depends(get_session)):
    o = await s.get(Odb, oid)
    if not o:
        raise HTTPException(404, "ODB not found")
    await _check_zone(s, b.zone_id)
    o.name, o.zone_id = b.name.strip(), b.zone_id
    await _commit(s, "ODB")
    return {"id": o.id, "name": o.name, "zone_id": o.zone_id}


@router.delete("/odbs/{oid}")
async def del_odb(oid: int, s: AsyncSession = Depends(get_session)):
    o = await s.get(Odb, oid)
    if not o:
        raise HTTPException(404, "ODB not found")
    await s.execute(update(ONU).where(ONU.odb_id == oid).values(odb_id=None))
    await s.delete(o)
    await s.commit()
    return {"ok": True}


# ───────────────────────── Speed profiles ─────────────────────────
class SpeedIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    direction: Literal["download", "upload"]
    kbps: int = Field(ge=64, le=10_000_000)
    is_default: bool = False


def _speed_dict(p: SpeedProfile, n: int = 0) -> dict:
    return {"id": p.id, "name": p.name, "direction": p.direction, "kbps": p.kbps, "kind": p.kind,
            "is_default": bool(p.is_default), "onus": n}


@router.get("/speed-profiles")
async def speed_profiles(direction: str = "", s: AsyncSession = Depends(get_session)):
    dl = dict((await s.execute(select(ONU.dl_profile_id, func.count()).where(ONU.dl_profile_id.is_not(None))
                               .group_by(ONU.dl_profile_id))).all())
    ul = dict((await s.execute(select(ONU.ul_profile_id, func.count()).where(ONU.ul_profile_id.is_not(None))
                               .group_by(ONU.ul_profile_id))).all())
    q = select(SpeedProfile).order_by(SpeedProfile.kbps, SpeedProfile.name)
    if direction in ("download", "upload"):
        q = q.where(SpeedProfile.direction == direction)
    return [_speed_dict(p, (dl if p.direction == "download" else ul).get(p.id, 0))
            for p in (await s.scalars(q)).all()]


async def _unset_default(s, direction, keep_id=None):
    q = update(SpeedProfile).where(SpeedProfile.direction == direction)
    if keep_id:
        q = q.where(SpeedProfile.id != keep_id)
    await s.execute(q.values(is_default=False))


@router.post("/speed-profiles", status_code=201)
async def add_speed(b: SpeedIn, background_tasks: BackgroundTasks, user: str = Depends(require_user),
                    s: AsyncSession = Depends(get_session)):
    p = SpeedProfile(name=b.name.strip(), direction=b.direction, kbps=b.kbps, is_default=b.is_default)
    s.add(p)
    try:
        await s.flush()
        if b.is_default:
            await _unset_default(s, b.direction, p.id)
    except IntegrityError:
        await s.rollback()
        raise HTTPException(409, "Speed profile already exists")
    await _commit(s, "Speed profile")
    target_count = await s.scalar(select(func.count(OLT.id)))
    if target_count:
        background_tasks.add_task(sync.push_speed_profile_to_all_olts, p.id, user)
    return {**_speed_dict(p), "push_targets": target_count or 0}


@router.put("/speed-profiles/{pid}")
async def edit_speed(pid: int, b: SpeedIn, s: AsyncSession = Depends(get_session)):
    p = await s.get(SpeedProfile, pid)
    if not p:
        raise HTTPException(404, "Speed profile not found")
    try:
        p.name, p.direction, p.kbps, p.is_default = b.name.strip(), b.direction, b.kbps, b.is_default
        await s.flush()
        if b.is_default:
            await _unset_default(s, b.direction, p.id)
    except IntegrityError:
        await s.rollback()
        raise HTTPException(409, "Speed profile already exists")
    await _commit(s, "Speed profile")
    return _speed_dict(p)


@router.delete("/speed-profiles/{pid}")
async def del_speed(pid: int, s: AsyncSession = Depends(get_session)):
    p = await s.get(SpeedProfile, pid)
    if not p:
        raise HTTPException(404, "Speed profile not found")
    col = ONU.dl_profile_id if p.direction == "download" else ONU.ul_profile_id
    await s.execute(update(ONU).where(col == pid).values(**{col.key: None}))
    await s.delete(p)
    await s.commit()
    return {"ok": True}


@router.get("/speed-profiles-export")
async def export_speed(s: AsyncSession = Depends(get_session)):
    rows = await speed_profiles("", s)
    return _csv([[r["name"], r["direction"], r["kbps"], r["is_default"], r["onus"]] for r in rows],
                ["Name", "Direction", "kbps", "Default", "ONUs"], "speed-profiles.csv")


# ───────────────────────── ONU types ─────────────────────────
class OnuTypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    pon_type: Literal["GPON", "EPON"] = "GPON"
    channels: str = Field("G", max_length=20)
    eth_ports: int = Field(4, ge=0, le=48)
    wifi_ports: int = Field(0, ge=0, le=16)
    voip_ports: int = Field(0, ge=0, le=8)
    catv: int = Field(0, ge=0, le=1)
    allow_custom: bool = True
    capability: Literal["Bridging/Routing", "Bridging", "Routing"] = "Bridging/Routing"


def _type_dict(t: OnuType, n: int = 0) -> dict:
    return {"id": t.id, "name": t.name, "pon_type": t.pon_type, "channels": t.channels,
            "eth_ports": t.eth_ports, "wifi_ports": t.wifi_ports, "voip_ports": t.voip_ports, "catv": t.catv,
            "allow_custom": bool(t.allow_custom), "capability": t.capability, "onus": n}


@router.get("/onu-types")
async def onu_types(pon_type: str = "", q: str = "", s: AsyncSession = Depends(get_session)):
    cnt = dict((await s.execute(select(ONU.onu_type, func.count()).group_by(ONU.onu_type))).all())
    qry = select(OnuType).order_by(OnuType.name)
    if pon_type:
        qry = qry.where(OnuType.pon_type == pon_type)
    if q.strip():
        qry = qry.where(OnuType.name.ilike("%" + q.strip().replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
    return [_type_dict(t, cnt.get(t.name, 0)) for t in (await s.scalars(qry)).all()]


@router.post("/onu-types", status_code=201)
async def add_type(b: OnuTypeIn, s: AsyncSession = Depends(get_session)):
    t = OnuType(**{**b.model_dump(), "name": b.name.strip()})
    s.add(t)
    await _commit(s, "ONU type")
    return _type_dict(t)


@router.put("/onu-types/{tid}")
async def edit_type(tid: int, b: OnuTypeIn, s: AsyncSession = Depends(get_session)):
    t = await s.get(OnuType, tid)
    if not t:
        raise HTTPException(404, "ONU type not found")
    for k, v in b.model_dump().items():
        setattr(t, k, v.strip() if k == "name" else v)
    await _commit(s, "ONU type")
    return _type_dict(t)


@router.delete("/onu-types/{tid}")
async def del_type(tid: int, s: AsyncSession = Depends(get_session)):
    t = await s.get(OnuType, tid)
    if not t:
        raise HTTPException(404, "ONU type not found")
    await s.delete(t)
    await s.commit()
    return {"ok": True}


@router.post("/onu-types/import-from-onus")
async def types_from_onus(s: AsyncSession = Depends(get_session)):
    """Create entries for ONU types used by registered ONUs but not yet in the list."""
    have = set(await s.scalars(select(OnuType.name)))
    names = {n for n in await s.scalars(select(ONU.onu_type).distinct()) if n and n not in have}
    s.add_all(OnuType(name=n[:60]) for n in sorted(names))
    await _commit(s, "ONU type")
    return {"created": len(names)}


@router.get("/onu-types-export")
async def export_types(s: AsyncSession = Depends(get_session)):
    rows = await onu_types("", "", s)
    return _csv([[r["pon_type"], r["channels"], r["name"], r["onus"], r["eth_ports"], r["wifi_ports"],
                  r["voip_ports"], r["catv"], r["capability"]] for r in rows],
                ["PON type", "Channels", "ONU type", "ONUs", "Ethernet", "WiFi", "VoIP", "CATV", "Capability"],
                "onu-types.csv")


# ───────────────────────── daftar ONU berfilter (Configured & Diagnostics) ─────────────────────────
class OnuFilter:
    def __init__(self, olt_id: Optional[int] = None, slot: Optional[int] = None, pon: Optional[int] = None,
                 zone_id: Optional[int] = None, odb_id: Optional[int] = None, vlan: Optional[int] = None,
                 onu_type: str = "", profile_id: Optional[int] = None, status: str = "", signal: str = "",
                 q: str = "", changed_from: str = "", changed_to: str = "", sort: str = ""):
        self.__dict__.update(locals())


def _local_day(d: str, end: bool = False) -> datetime:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "Date format must be YYYY-MM-DD")
    if end:
        dt += timedelta(days=1)
    return dt.replace(tzinfo=ZoneInfo(settings.timezone)).astimezone(timezone.utc).replace(tzinfo=None)


def _like(text: str) -> str:
    return "%" + text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _conds(f: OnuFilter, with_status: bool = True) -> list:
    c = []
    for val, col in ((f.olt_id, ONU.olt_id), (f.slot, ONU.slot), (f.pon, ONU.pon), (f.zone_id, ONU.zone_id),
                     (f.odb_id, ONU.odb_id), (f.vlan, ONU.vlan)):
        if val is not None:
            c.append(col == val)
    if f.onu_type:
        c.append(ONU.onu_type == f.onu_type)
    if f.profile_id:
        c.append(or_(ONU.dl_profile_id == f.profile_id, ONU.ul_profile_id == f.profile_id))
    if with_status and f.status and f.status != "all":
        c.append(ONU.status.in_([x for x in f.status.split(",") if x]))
    if f.signal:
        sel = set(f.signal.split(","))
        warn, crit = func.coalesce(OLT.rx_warn, -30.0), func.coalesce(OLT.rx_crit, -32.0)
        opts = []
        if "good" in sel:
            opts.append(ONU.rx_power >= warn)
        if "warn" in sel:
            opts.append(and_(ONU.rx_power < warn, ONU.rx_power >= crit))
        if "crit" in sel:
            opts.append(ONU.rx_power < crit)
        if opts:
            c.append(or_(*opts))
    if f.q.strip():
        like = _like(f.q)
        text_matches = [col.ilike(like, escape="\\") for col in
                        (ONU.name, ONU.sn, ONU.pppoe_user, ONU.description, ONU.address, ONU.external_id, ONU.contact)]
        compact_mac = re.sub(r"[^0-9A-Fa-f]", "", f.q)
        if len(compact_mac) >= 2 and re.fullmatch(r"[0-9A-Fa-f:.\-\s]+", f.q):
            text_matches.append(
                select(OnuMacAddress.id).where(
                    OnuMacAddress.onu_id == ONU.id,
                    OnuMacAddress.mac_compact.ilike(_like(compact_mac), escape="\\"),
                ).exists()
            )
        else:
            text_matches.append(
                select(OnuMacAddress.id).where(
                    OnuMacAddress.onu_id == ONU.id,
                    OnuMacAddress.mac.ilike(like, escape="\\"),
                ).exists()
            )
        c.append(or_(*text_matches))
    if f.changed_from:
        c.append(ONU.status_changed_at >= _local_day(f.changed_from))
    if f.changed_to:
        c.append(ONU.status_changed_at < _local_day(f.changed_to, end=True))
    return c


def _order(sort: str):
    return {"changed": [ONU.status_changed_at.desc().nulls_last()], "rx": [ONU.rx_power.asc().nulls_last()],
            "name": [ONU.name, ONU.id], "status": [ONU.status, OLT.name, ONU.slot, ONU.pon, ONU.onu_number]
            }.get(sort, [OLT.name, ONU.slot, ONU.pon, ONU.onu_number])


def _base_select():
    dl, ul = aliased(SpeedProfile), aliased(SpeedProfile)
    st = (select(ONU, OLT, Zone.name, Odb.name, dl.name, ul.name)
          .join(OLT, OLT.id == ONU.olt_id)
          .outerjoin(Zone, Zone.id == ONU.zone_id).outerjoin(Odb, Odb.id == ONU.odb_id)
          .outerjoin(dl, dl.id == ONU.dl_profile_id).outerjoin(ul, ul.id == ONU.ul_profile_id))
    return st


def _row(o: ONU, olt: OLT, zone, odb, dl, ul) -> dict:
    d = onu_dict(o, olt.name)
    warn = olt.rx_warn if olt.rx_warn is not None else -30.0
    crit = olt.rx_crit if olt.rx_crit is not None else -32.0
    sig = None if o.rx_power is None else "good" if o.rx_power >= warn else "warn" if o.rx_power >= crit else "crit"
    d.update(zone=zone, odb=odb, dl_profile=dl, ul_profile=ul, zone_id=o.zone_id, odb_id=o.odb_id,
             address=o.address or "", contact=o.contact or "", external_id=o.external_id or "",
             auth_date=sync.iso(o.auth_date), mode=o.mode or "", distance_m=o.distance_m, signal=sig,
             dl_profile_id=o.dl_profile_id, ul_profile_id=o.ul_profile_id, rx_warn=warn, rx_crit=crit)
    return d


@router.get("/onus")
async def list_onus(f: OnuFilter = Depends(), page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=200),
                    s: AsyncSession = Depends(get_session)):
    conds = _conds(f)
    total = (await s.execute(select(func.count(ONU.id)).select_from(ONU).join(OLT, OLT.id == ONU.olt_id)
                             .where(*conds))).scalar_one()
    rows = (await s.execute(_base_select().where(*conds).order_by(*_order(f.sort))
                            .offset((page - 1) * per_page).limit(per_page))).all()
    by = (await s.execute(select(ONU.status, func.count()).select_from(ONU).join(OLT, OLT.id == ONU.olt_id)
                          .where(*_conds(f, with_status=False)).group_by(ONU.status))).all()
    counts = {k: 0 for k in sync.STATUSES}
    counts.update(dict(by))
    counts["total"] = sum(counts.values())
    return {"total": total, "page": page, "per_page": per_page, "counts": counts,
            "items": [_row(*r) for r in rows]}


@router.get("/onus-export")
async def export_onus(f: OnuFilter = Depends(), s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(_base_select().where(*_conds(f)).order_by(*_order(f.sort)).limit(100_000))).all()
    out = []
    for r in rows:
        d = _row(*r)
        out.append([d["olt_name"], d["location"], d["sn"], d["name"], d["onu_type"], d["zone"], d["odb"], d["vlan"],
                    d["status"], d["rx_power"], d["olt_rx_power"], d["dl_profile"], d["ul_profile"], d["address"],
                    d["contact"], d["external_id"], d["auth_date"]])
    return _csv(out, ["OLT", "ONU", "SN", "Name", "Type", "Zone", "ODB", "VLAN", "Status", "Rx ONU dBm",
                      "Rx OLT dBm", "Download", "Upload", "Address", "Contact", "External ID", "Auth date"],
                "onus.csv")


@router.get("/filters")
async def filter_options(olt_id: Optional[int] = None, s: AsyncSession = Depends(get_session)):
    olts = [{"id": o.id, "name": o.name} for o in (await s.scalars(select(OLT).order_by(OLT.name))).all()]
    port_q = select(ONU.slot, ONU.pon).distinct().order_by(ONU.slot, ONU.pon)
    if olt_id:
        port_q = port_q.where(ONU.olt_id == olt_id)
    ports = (await s.execute(port_q)).all()
    vlans = [v for v in await s.scalars(select(ONU.vlan).where(ONU.vlan.is_not(None)).distinct().order_by(ONU.vlan))]
    types = sorted(set(await s.scalars(select(OnuType.name))) | {t for t in await s.scalars(select(ONU.onu_type).distinct()) if t})
    return {"olts": olts, "boards": sorted({p[0] for p in ports}),
            "ports": [{"slot": a, "pon": b} for a, b in ports], "vlans": vlans, "onu_types": types,
            "zones": [{"id": z.id, "name": z.name} for z in (await s.scalars(select(Zone).order_by(Zone.name))).all()],
            "odbs": [{"id": o.id, "name": o.name} for o in (await s.scalars(select(Odb).order_by(Odb.name))).all()],
            "profiles": [{"id": p.id, "name": p.name, "direction": p.direction}
                         for p in (await s.scalars(select(SpeedProfile).order_by(SpeedProfile.name))).all()]}


class OnuEdit(BaseModel):
    """Dashboard-only data. OLT CLI changes are handled through preview/execution in /api/plan and /api/exec."""
    name: Optional[str] = Field(None, max_length=100)
    address: Optional[str] = Field(None, max_length=300)
    contact: Optional[str] = Field(None, max_length=100)
    external_id: Optional[str] = Field(None, max_length=64)
    mode: Optional[Literal["", "routing", "bridge"]] = None
    zone_id: Optional[int] = None
    odb_id: Optional[int] = None
    dl_profile_id: Optional[int] = None
    ul_profile_id: Optional[int] = None


@router.put("/onus/{onu_id}")
async def edit_onu(onu_id: int, b: OnuEdit, s: AsyncSession = Depends(get_session)):
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found")
    data = b.model_dump(exclude_unset=True)
    for key, model in (("zone_id", Zone), ("odb_id", Odb)):
        if data.get(key) is not None and not await s.get(model, data[key]):
            raise HTTPException(422, f"{key} does not exist")
    for key, direction in (("dl_profile_id", "download"), ("ul_profile_id", "upload")):
        if data.get(key) is not None:
            p = await s.get(SpeedProfile, data[key])
            if not p or p.direction != direction:
                raise HTTPException(422, f"{key} is not a {direction} profile")
    for k, v in data.items():
        setattr(onu, k, v)
    await s.commit()
    return {"ok": True}


# ───────────────────────── info OLT: cards / PON / uplink / VLAN ─────────────────────────
class OltSettings(BaseModel):
    rx_warn: float = Field(ge=-60, le=0)
    rx_crit: float = Field(ge=-60, le=0)
    temp_warn: float = Field(ge=0, le=150)
    temp_crit: float = Field(ge=0, le=150)


@router.put("/olts/{olt_id}/settings")
async def olt_settings(olt_id: int, b: OltSettings, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    if b.rx_crit >= b.rx_warn:
        raise HTTPException(422, "The critical threshold must be lower than the warning threshold")
    if b.temp_crit <= b.temp_warn:
        raise HTTPException(422, "The critical temperature must be higher than the warning threshold")
    olt.rx_warn, olt.rx_crit, olt.temp_warn, olt.temp_crit = b.rx_warn, b.rx_crit, b.temp_warn, b.temp_crit
    await s.commit()
    return {"ok": True}


@router.get("/olts/{olt_id}/settings")
async def get_olt_settings(olt_id: int, s: AsyncSession = Depends(get_session)):
    o = await _get_olt(s, olt_id)
    return {"rx_warn": o.rx_warn, "rx_crit": o.rx_crit, "temp_warn": o.temp_warn, "temp_crit": o.temp_crit}


@router.get("/olts/{olt_id}/cards")
async def olt_cards(olt_id: int, s: AsyncSession = Depends(get_session)):
    await _get_olt(s, olt_id)
    data, ts = await sync.load_info(s, olt_id, "cards")
    return {"rows": data.get("rows", []), "raw": data.get("raw", ""), "updated_at": sync.iso(ts)}


@router.post("/olts/{olt_id}/cards/refresh")
async def refresh_cards(olt_id: int, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    data = await _guard(zte.read_cards(zte.conn_from_row(olt)))
    if data["ok"]:
        await sync.save_info(s, olt_id, "cards", {"rows": data["rows"], "raw": data["raw"]})
        await s.commit()
    return {"rows": data["rows"], "raw": data["raw"], "ok": data["ok"], "updated_at": sync.iso(utcnow())}


@router.get("/olts/{olt_id}/pon-ports")
async def olt_pon_ports(olt_id: int, s: AsyncSession = Depends(get_session)):
    """Calculated from ONU database data and running-config descriptions/admin state. Tx power and link status are unavailable."""
    await _get_olt(s, olt_id)
    cfg, ts = await sync.load_info(s, olt_id, "pon_cfg")
    cards, _ = await sync.load_info(s, olt_id, "cards")
    board = {c["slot"]: c["real_type"] or c["type"] for c in cards.get("rows", [])}
    stats = {(r[0], r[1], r[2]): r for r in (await s.execute(
        select(ONU.shelf, ONU.slot, ONU.pon, func.count(),
               func.sum(case((ONU.status == "online", 1), else_=0)),
               func.avg(case((ONU.status == "online", ONU.rx_power), else_=None)))
        .where(ONU.olt_id == olt_id).group_by(ONU.shelf, ONU.slot, ONU.pon))).all()}
    keys = set(stats) | {tuple(int(x) for x in k.split("/")) for k in cfg}
    slots: dict[int, list] = {}
    for shelf, slot, pon in sorted(keys):
        st = stats.get((shelf, slot, pon))
        c = cfg.get(f"{shelf}/{slot}/{pon}", {})
        slots.setdefault(slot, []).append({
            "shelf": shelf, "slot": slot, "port": pon, "type": "GPON", "admin": c.get("admin", "Enabled"),
            "status": None, "online": int(st[4] or 0) if st else 0, "total": st[3] if st else 0, "capacity": 128,
            "avg_rx": round(st[5], 2) if st and st[5] is not None else None,
            "description": c.get("description", "")})
    return {"slots": [{"slot": sl, "board_type": board.get(sl, ""), "ports": ports} for sl, ports in sorted(slots.items())],
            "updated_at": sync.iso(ts)}


@router.get("/olts/{olt_id}/uplinks")
async def olt_uplinks(olt_id: int, live: bool = False, optical: bool = False, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    cfg, ts = await sync.load_info(s, olt_id, "uplinks")
    rows = {n: {"name": n, "description": c.get("description", ""), "admin": c.get("admin", ""),
                "mode": c.get("mode", ""), "vlans": c.get("vlans", ""), "pvid": c.get("pvid", ""),
                "status": None, "speed_mbps": None, "mtu": None, "rx": None, "tx": None, "temp": None, "wavelength": None}
            for n, c in cfg.items()}
    live_error = ""
    if live:
        try:
            table = await zte.snmp_if_table(zte.conn_from_row(olt))
            for name, r in table.items():
                if not graphs.IF_KEEP.match(name) or graphs.is_pon(name):
                    continue
                row = rows.setdefault(name, {"name": name, "description": "", "admin": "", "mode": "", "vlans": "", "pvid": "",
                                             "status": None, "speed_mbps": None, "mtu": None, "rx": None, "tx": None,
                                             "temp": None, "wavelength": None})
                row["status"] = {1: "Up", 2: "Down"}.get(r.get("oper"))
                row["speed_mbps"], row["mtu"] = r.get("speed"), r.get("mtu")
                if not row["admin"] and r.get("admin"):
                    row["admin"] = {1: "Enabled", 2: "Disabled"}.get(r["admin"], "")
        except (SnmpError, ValueError) as exc:
            live_error = str(exc)
    if optical:
        try:
            ports = [n for n, r in rows.items() if r["admin"] != "Disabled"]
            for name, d in (await zte.read_optical(zte.conn_from_row(olt), ports)).items():
                rows[name].update(rx=d["rx"], tx=d["tx"], temp=d["temp"], wavelength=d["wavelength"])
        except Exception as exc:   # Optical data is optional; do not fail the uplink tab.
            live_error = (live_error + "; " if live_error else "") + f"optik: {exc}"
    return {"rows": sorted(rows.values(), key=lambda r: r["name"]), "updated_at": sync.iso(ts), "live_error": live_error}


@router.get("/olts/{olt_id}/vlans")
async def olt_vlans(olt_id: int, s: AsyncSession = Depends(get_session)):
    await _get_olt(s, olt_id)
    cfg, ts = await sync.load_info(s, olt_id, "vlans")
    cnt = dict((await s.execute(select(ONU.vlan, func.count()).where(ONU.olt_id == olt_id, ONU.vlan.is_not(None))
                                .group_by(ONU.vlan))).all())
    ids = {int(k) for k in cfg} | set(cnt)
    return {"rows": [{"vlan": i, "name": cfg.get(str(i), {}).get("name", ""), "onus": cnt.get(i, 0),
                      "in_config": str(i) in cfg} for i in sorted(ids)], "updated_at": sync.iso(ts)}


@router.get("/olts/{olt_id}/onu-types")
async def olt_onu_types(olt_id: int, live: bool = False, s: AsyncSession = Depends(get_session)):
    """Options for the Authorize form: types defined on this OLT (running-config + `show onu-type gpon`)
    and types already used by this OLT's ONUs first, followed by the remaining Settings → ONU types.
    With live=true, read from the OLT now (Telnet) and save the results."""
    olt = await _get_olt(s, olt_id)
    live_msg = ""
    if live:
        try:
            conn = zte.conn_from_row(olt)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        r = await _guard(zte.read_onu_types(conn))
        if r["types"]:
            cfg0, _ = await sync.load_info(s, olt_id, "onu_types")
            await sync.save_info(s, olt_id, "onu_types", {**cfg0, **r["types"]})
            await s.commit()
            await sync.import_masters({"onu_types": r["types"]})
            live_msg = f"Read {len(r['types'])} types from OLT"
        else:
            live_msg = ("OLT rejected command " if not r["ok"] else "Unrecognized output format: ") + f"`{r.get('command', '')}`"
    cfg, _ = await sync.load_info(s, olt_id, "onu_types")
    used = {t for t in await s.scalars(select(ONU.onu_type).where(ONU.olt_id == olt_id).distinct()) if t}
    master = {t.name: t for t in (await s.scalars(select(OnuType).order_by(OnuType.name))).all()}
    rows = []
    for name in sorted(set(cfg) | used, key=str.lower):
        m = master.get(name)
        rows.append({"name": name, "on_olt": True, "eth_ports": m.eth_ports if m else cfg.get(name, {}).get("eth"),
                     "description": (cfg.get(name) or {}).get("description", "")})
    for name in sorted(set(master) - {r["name"] for r in rows}, key=str.lower):
        rows.append({"name": name, "on_olt": False, "eth_ports": master[name].eth_ports, "description": ""})
    return {"rows": rows, "live_msg": live_msg}


# ───────────────────────── Unconfigured (read-only) ─────────────────────────
@router.get("/unconfigured")
async def unconfigured(olt_id: Optional[int] = None, s: AsyncSession = Depends(get_session)):
    q = select(OLT).order_by(OLT.name)
    if olt_id:
        q = q.where(OLT.id == olt_id)
    olts = (await s.scalars(q)).all()
    if olt_id and not olts:
        raise HTTPException(404, "OLT not found")

    async def one(o: OLT):
        try:
            return o, await zte.scan_uncfg(zte.conn_from_row(o)), None
        except Exception as exc:  # One OLT failure must not prevent processing the others.
            return o, None, str(exc) or exc.__class__.__name__

    items, errors, raw = [], [], {}
    for o, res, err in await asyncio.gather(*(one(o) for o in olts)):
        if err:
            errors.append({"olt": o.name, "error": err})
            continue
        raw[o.name] = res["raw"]
        if not res["ok"]:
            errors.append({"olt": o.name, "error": "OLT rejected the scan command: " + res["raw"][:80]})
        items += [{"olt_id": o.id, "olt_name": o.name,
                   "location": f"{r['shelf']}/{r['slot']}/{r['pon']}" + (f":{r['index']}" if r["index"] else ""),
                   "slot": r["slot"], "pon": r["pon"], "sn": r["sn"], "state": r["state"],
                   "model": r.get("model", "")} for r in res["rows"]]
    return {"items": items, "errors": errors, "raw": raw, "scanned": len(olts)}


@router.post("/unconfigured/registered")
async def unconfigured_registered(b: UnconfiguredSerialsIn, s: AsyncSession = Depends(get_session)):
    serials = {sn.strip().upper() for sn in b.sns if sn and sn.strip()}
    registered = {}
    if serials:
        rows = (await s.execute(
            select(ONU, OLT.name).join(OLT, OLT.id == ONU.olt_id)
            .where(func.upper(func.trim(ONU.sn)).in_(serials))
            .order_by(OLT.name, ONU.slot, ONU.pon, ONU.onu_number)
        )).all()
        for onu, olt_name in rows:
            registered.setdefault(onu.sn.strip().upper(), []).append({
                "id": onu.id, "olt_id": onu.olt_id, "olt": olt_name, "sn": onu.sn,
                "name": onu.name, "slot": onu.slot, "pon": onu.pon, "onu_number": onu.onu_number,
            })
    return {"registered": registered}


# ───────────────────────── Graphs ─────────────────────────
@router.get("/graphs")
async def graph_data(olt_id: int, metric: Literal["if_in", "if_out", "pon_rx", "onu_status", "up_rx", "up_tx", "up_temp"],
                     hours: int = Query(24, ge=1, le=24 * 14), s: AsyncSession = Depends(get_session)):
    await _get_olt(s, olt_id)
    cutoff = utcnow() - timedelta(hours=hours)
    rows = (await s.execute(select(GraphSample.key, GraphSample.ts, GraphSample.value)
                            .where(GraphSample.olt_id == olt_id, GraphSample.metric == metric, GraphSample.ts >= cutoff)
                            .order_by(GraphSample.key, GraphSample.ts))).all()
    series: dict[str, list] = {}
    for key, ts, v in rows:
        series.setdefault(key, []).append([sync.iso(ts), round(v, 2)])
    return {"series": series, "hours": hours}
