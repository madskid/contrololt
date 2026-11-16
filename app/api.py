"""REST API + WebSocket."""
import asyncio
import re
from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bus import bus
from app.config import settings
from app.db import SessionLocal, get_session
from app.drivers import cdata, hioso, zte
from app.drivers.snmp import SnmpError
from app.drivers.telnet import BusyError, TelnetError
from app.models import MikroRouter, OLT, ONU, User, utcnow
from app.security import enc, require_user, user_olt_ids
from app.services import sync

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])
ws_router = APIRouter()
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,62}$")
EMPTY_COUNTS = {**{k: 0 for k in sync.STATUSES}, "total": 0}


async def _visible_counts(s: AsyncSession, allowed_olt_ids: set[int] | None) -> dict[int, dict]:
    if allowed_olt_ids is None:
        return await sync.counts_for(s)
    if not allowed_olt_ids:
        return {}
    rows = (await s.execute(
        select(ONU.olt_id, ONU.status, func.count()).where(
            ONU.hidden_duplicate.is_(False), ONU.olt_id.in_(allowed_olt_ids))
        .group_by(ONU.olt_id, ONU.status)
    )).all()
    counts = {}
    for olt_id, status, count in rows:
        item = counts.setdefault(olt_id, {**EMPTY_COUNTS})
        item[status] = count
    for item in counts.values():
        item["total"] = sum(item[key] for key in sync.STATUSES)
    return counts


# ───────────────────────── schema ─────────────────────────
class OltIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    ip: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=50, description="Preset (C300/C320/...) or custom text")
    vendor: Literal["zte", "hioso", "cdata"] = "zte"
    cli_family: Optional[Literal["legacy", "c6xx"]] = None   # Required only for a custom model.
    shelf: int = Field(1, ge=0, le=16)
    telnet_port: int = Field(23, ge=1, le=65535)
    telnet_user: str = Field("", max_length=100)
    telnet_pass: str = Field("", max_length=200)
    enable_pass: str = Field("", max_length=200)
    snmp_community: str = Field("public", min_length=1, max_length=100)
    snmp_port: int = Field(161, ge=1, le=65535)
    mikrotik_router_id: Optional[int] = Field(None, gt=0)
    write_enabled: bool = False

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        v = v.strip()
        if not HOST_RE.match(v):
            raise ValueError("Invalid IP address or hostname")
        return v

    @field_validator("name", "model")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Cannot be empty")
        return v

    @field_validator("telnet_user")
    @classmethod
    def _strip_telnet_user(cls, v: str) -> str:
        return v.strip()


class OltUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    ip: Optional[str] = None
    model: Optional[str] = Field(None, min_length=1, max_length=50)
    vendor: Optional[Literal["zte", "hioso", "cdata"]] = None
    cli_family: Optional[Literal["legacy", "c6xx"]] = None
    shelf: Optional[int] = Field(None, ge=0, le=16)
    telnet_port: Optional[int] = Field(None, ge=1, le=65535)
    telnet_user: Optional[str] = Field(None, min_length=1, max_length=100)
    telnet_pass: Optional[str] = Field(None, max_length=200)      # kosongin gapapa dah
    enable_pass: Optional[str] = Field(None, max_length=200)
    snmp_community: Optional[str] = Field(None, max_length=100)
    snmp_port: Optional[int] = Field(None, ge=1, le=65535)
    mikrotik_router_id: Optional[int] = Field(None, gt=0)
    write_enabled: Optional[bool] = None

    @field_validator("ip")
    @classmethod
    def _ip(cls, v):
        if v is not None:
            v = v.strip()
            if not HOST_RE.match(v):
                raise ValueError("Invalid IP address or hostname")
        return v


def _family(model: str, requested: str | None) -> str:
    return zte.family_for_model(model) or requested or "legacy"


def olt_dict(o: OLT, counts: dict | None = None) -> dict:
    return {"id": o.id, "name": o.name, "ip": o.ip, "model": o.model, "vendor": o.vendor, "cli_family": o.cli_family,
            "shelf": o.shelf, "telnet_port": o.telnet_port, "telnet_user": o.telnet_user,
            "telnet_configured": bool(o.telnet_pass_enc),
            "snmp_port": o.snmp_port, "mikrotik_router_id": o.mikrotik_router_id,
            "sw_version": o.sw_version or "", "write_enabled": bool(o.write_enabled), "hw_version": f"{o.vendor.upper()}-{o.model}" if o.model else "", "status": o.status, "status_msg": o.status_msg,
            "sys_descr": o.sys_descr, "uptime_s": o.uptime_s, "poll_source": o.poll_source,
            "last_poll_at": sync.iso(o.last_poll_at), "last_full_sync_at": sync.iso(o.last_full_sync_at),
            "last_save_at": sync.iso(o.last_save_at), "last_save_ok": o.last_save_ok,
            "last_save_msg": o.last_save_msg, "counts": counts or dict(EMPTY_COUNTS)}


def onu_dict(o: ONU, olt_name: str | None = None) -> dict:
    return {"id": o.id, "olt_id": o.olt_id, "olt_name": olt_name,
            "location": f"{o.shelf}/{o.slot}/{o.pon}:{o.onu_number}",
            "shelf": o.shelf, "slot": o.slot, "pon": o.pon, "onu_number": o.onu_number,
            "sn": o.sn, "onu_type": o.onu_type, "name": o.name, "description": o.description,
            "vlan": o.vlan, "conn_type": o.conn_type, "pppoe_user": o.pppoe_user,
            "status": o.status, "phase_state": o.phase_state, "rx_power": o.rx_power,
            "olt_rx_power": o.olt_rx_power, "status_changed_at": sync.iso(o.status_changed_at),
            "updated_at": sync.iso(o.updated_at)}


async def _get_olt(s: AsyncSession, olt_id: int) -> OLT:
    olt = await s.get(OLT, olt_id)
    if not olt:
        raise HTTPException(404, "OLT not found")
    return olt


# ───────────────────────── meta & ringkasan ─────────────────────────
@router.get("/meta")
async def meta():
    return {"models": [*zte.MODEL_PRESETS, {"key": "HA7308C", "label": "HIOSO HA7308C", "vendor": "hioso"},
                       {"key": "FD1304E-B1-PDX0", "label": "C-Data FD1304E-B1-PDX0 (EPON)", "vendor": "cdata"}],
            "families": zte.FAMILY_LABELS,
            "autosave_time": settings.autosave_time, "timezone": settings.timezone,
            "poll_interval": settings.poll_interval}


@router.get("/summary")
async def summary(request: Request, s: AsyncSession = Depends(get_session)):
    allowed_olt_ids = getattr(request.state, "allowed_olt_ids", None)
    counts = await _visible_counts(s, allowed_olt_ids)
    total = {**EMPTY_COUNTS}
    for c in counts.values():
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
    olt_query = select(func.count(OLT.id))
    status_query = select(OLT.status, func.count()).group_by(OLT.status)
    if allowed_olt_ids is not None:
        olt_query = olt_query.where(OLT.id.in_(allowed_olt_ids))
        status_query = status_query.where(OLT.id.in_(allowed_olt_ids))
    n_olt = (await s.execute(olt_query)).scalar_one()
    by_status = dict((await s.execute(status_query)).all())
    return {"olts": n_olt, "olts_by_status": by_status, "onus": total}


# ───────────────────────── OLT CRUD ─────────────────────────
@router.get("/olts")
async def list_olts(request: Request, s: AsyncSession = Depends(get_session)):
    allowed_olt_ids = getattr(request.state, "allowed_olt_ids", None)
    counts = await _visible_counts(s, allowed_olt_ids)
    olt_query = select(OLT).order_by(OLT.name)
    if allowed_olt_ids is not None:
        olt_query = olt_query.where(OLT.id.in_(allowed_olt_ids))
        counts = {olt_id: count for olt_id, count in counts.items() if olt_id in allowed_olt_ids}
    olts = (await s.scalars(olt_query)).all()
    rows = [olt_dict(o, counts.get(o.id)) for o in olts]
    if allowed_olt_ids is not None:
        for row in rows:
            row.pop("telnet_user", None)
            row.pop("ip", None)
    return rows


@router.post("/olts", status_code=201)
async def add_olt(body: OltIn, s: AsyncSession = Depends(get_session)):
    if body.vendor == "hioso" and body.model != "HA7308C":
        raise HTTPException(422, "Only the HIOSO HA7308C model is currently supported.")
    if body.model == "HA7308C" and body.vendor != "hioso":
        raise HTTPException(422, "HA7308C must use the HIOSO vendor driver.")
    if body.vendor == "cdata" and body.model != "FD1304E-B1-PDX0":
        raise HTTPException(422, "Only the C-Data FD1304E-B1-PDX0 EPON model is currently supported.")
    if body.vendor == "cdata" and body.write_enabled:
        raise HTTPException(422, "C-Data currently supports read-only SNMP monitoring only.")
    if body.vendor == "cdata" and bool(body.telnet_user) != bool(body.telnet_pass):
        raise HTTPException(422, "C-Data Telnet username and password must be supplied together.")
    if body.vendor == "zte" and (not body.telnet_user or not body.telnet_pass):
        raise HTTPException(422, "Telnet username and password are required for ZTE OLTs.")
    if body.vendor == "hioso" and body.write_enabled and (not body.telnet_user or not body.telnet_pass):
        raise HTTPException(422, "HIOSO rename/reboot requires Telnet username and password.")
    if body.mikrotik_router_id is not None:
        if body.vendor != "cdata":
            raise HTTPException(422, "A MikroTik router can only be linked to a C-Data OLT.")
        if not await s.get(MikroRouter, body.mikrotik_router_id):
            raise HTTPException(422, "The selected MikroTik router does not exist.")
    if (await s.scalars(select(OLT).where(OLT.ip == body.ip))).first():
        raise HTTPException(409, f"An OLT with IP {body.ip} is already registered")
    olt = OLT(name=body.name, ip=body.ip, model=body.model, vendor=body.vendor,
              cli_family=_family(body.model, body.cli_family),
              shelf=body.shelf, telnet_port=body.telnet_port, telnet_user=body.telnet_user,
              telnet_pass_enc=enc(body.telnet_pass), enable_pass_enc=enc(body.enable_pass),
              snmp_community_enc=enc(body.snmp_community), snmp_port=body.snmp_port,
              mikrotik_router_id=body.mikrotik_router_id,
              write_enabled=body.write_enabled,
              status="pending", status_msg="Waiting for initial sync…", sync_requested=True)
    s.add(olt)
    await s.commit()
    await bus.publish(sync.olt_event(olt))
    return olt_dict(olt)


@router.post("/olts/test")
async def test_olt(body: OltIn):
    if body.vendor == "hioso":
        if body.model != "HA7308C":
            raise HTTPException(422, "Only the HIOSO HA7308C model is currently supported.")
        result = await hioso.probe(body.ip, body.snmp_community, body.snmp_port)
        if body.telnet_user and body.telnet_pass:
            try:
                result["telnet_ok"] = True
                result["telnet_msg"] = await hioso.test_telnet(
                    body.ip, body.telnet_port, body.telnet_user, body.telnet_pass, body.enable_pass)
            except (TelnetError, asyncio.TimeoutError, OSError) as exc:
                result["telnet_ok"], result["telnet_msg"] = False, str(exc)
        else:
            result["telnet_msg"] = "Telnet credentials not supplied; CLI actions remain unavailable."
        return result
    if body.vendor == "cdata":
        if body.model != "FD1304E-B1-PDX0":
            raise HTTPException(422, "Only the C-Data FD1304E-B1-PDX0 EPON model is currently supported.")
        if body.write_enabled:
            raise HTTPException(422, "C-Data currently supports read-only SNMP monitoring only.")
        result = await cdata.probe(body.ip, body.snmp_community, body.snmp_port)
        if body.telnet_user or body.telnet_pass:
            if not body.telnet_user or not body.telnet_pass:
                raise HTTPException(422, "Enter both C-Data Telnet username and password to test the CLI.")
            try:
                result["telnet_ok"] = True
                result["telnet_msg"] = await cdata.test_telnet(
                    body.ip, body.telnet_port, body.telnet_user, body.telnet_pass, body.enable_pass)
            except (TelnetError, asyncio.TimeoutError, OSError) as exc:
                result["telnet_ok"], result["telnet_msg"] = False, str(exc) or exc.__class__.__name__
        return result
    if not body.telnet_user or not body.telnet_pass:
        raise HTTPException(422, "Telnet username and password are required for ZTE OLTs.")
    conn = zte.OltConn(id=-1, name=body.name, ip=body.ip, telnet_port=body.telnet_port,
                       telnet_user=body.telnet_user, telnet_pass=body.telnet_pass,
                       enable_pass=body.enable_pass, snmp_community=body.snmp_community,
                       snmp_port=body.snmp_port, shelf=body.shelf, family=_family(body.model, body.cli_family))
    return await zte.probe(conn)


@router.post("/olts/save-all")
async def save_all():
    """Save the configuration on all OLTs now (the 'Save config' menu)."""
    from app.db import SessionLocal
    async with SessionLocal() as s:
        olts = (await s.execute(select(OLT.id, OLT.name).where(OLT.vendor == "zte"))).all()
    res = await asyncio.gather(*(sync.do_save(o.id) for o in olts), return_exceptions=True)
    out = []
    for o, r in zip(olts, res):
        ok, msg = (False, str(r)) if isinstance(r, Exception) else r
        out.append({"olt": o.name, "ok": ok, "message": msg})
    return {"results": out, "ok": all(x["ok"] for x in out)}


@router.get("/olts/{olt_id}")
async def get_olt(olt_id: int, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    return olt_dict(olt, (await sync.counts_for(s, olt_id)).get(olt_id))


@router.put("/olts/{olt_id}")
async def update_olt(olt_id: int, body: OltUpdate, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    data = body.model_dump(exclude_unset=True)
    conn_changed = False
    vendor = data.get("vendor", olt.vendor)
    model = data.get("model", olt.model)
    if vendor == "hioso" and model != "HA7308C":
        raise HTTPException(422, "Only the HIOSO HA7308C model is currently supported.")
    if model == "HA7308C" and vendor != "hioso":
        raise HTTPException(422, "HA7308C must use the HIOSO vendor driver.")
    if vendor == "cdata" and model != "FD1304E-B1-PDX0":
        raise HTTPException(422, "Only the C-Data FD1304E-B1-PDX0 EPON model is currently supported.")
    if vendor == "cdata" and data.get("write_enabled", olt.write_enabled):
        raise HTTPException(422, "C-Data currently supports read-only SNMP monitoring only.")
    if vendor == "cdata" and bool(data.get("telnet_user", olt.telnet_user)) != bool(
            data.get("telnet_pass") or olt.telnet_pass_enc):
        raise HTTPException(422, "C-Data Telnet username and password must be supplied together.")
    if vendor == "zte" and (not data.get("telnet_user", olt.telnet_user) or
                            (not olt.telnet_pass_enc and not data.get("telnet_pass"))):
        raise HTTPException(422, "Telnet username and password are required for ZTE OLTs.")
    if vendor == "hioso" and data.get("write_enabled", olt.write_enabled) and (
            not data.get("telnet_user", olt.telnet_user) or
            (not olt.telnet_pass_enc and not data.get("telnet_pass"))):
        raise HTTPException(422, "HIOSO rename/reboot requires Telnet username and password.")
    if "mikrotik_router_id" in data and data["mikrotik_router_id"] is not None:
        if vendor != "cdata":
            raise HTTPException(422, "A MikroTik router can only be linked to a C-Data OLT.")
        if not await s.get(MikroRouter, data["mikrotik_router_id"]):
            raise HTTPException(422, "The selected MikroTik router does not exist.")
    if vendor != olt.vendor:
        olt.vendor = vendor
        conn_changed = True
    if "ip" in data and data["ip"] != olt.ip:
        if (await s.scalars(select(OLT).where(OLT.ip == data["ip"]))).first():
            raise HTTPException(409, f"An OLT with IP {data['ip']} is already registered")
    if data.get("write_enabled") is not None:
        olt.write_enabled = data["write_enabled"]
    if "mikrotik_router_id" in data:
        olt.mikrotik_router_id = data["mikrotik_router_id"]
    elif vendor != "cdata":
        olt.mikrotik_router_id = None
    for f in ("name", "ip", "model", "shelf", "telnet_port", "telnet_user", "snmp_port"):
        if f in data and data[f] is not None and getattr(olt, f) != data[f]:
            setattr(olt, f, data[f])
            conn_changed = conn_changed or f not in ("name",)
    if "model" in data or "cli_family" in data:
        fam = _family(olt.model, data.get("cli_family") or olt.cli_family)
        conn_changed = conn_changed or fam != olt.cli_family
        olt.cli_family = fam
    for f, col in (("telnet_pass", "telnet_pass_enc"), ("enable_pass", "enable_pass_enc"),
                   ("snmp_community", "snmp_community_enc")):
        if data.get(f):                      # Empty/None preserves the existing value
            setattr(olt, col, enc(data[f]))
            conn_changed = True
    if conn_changed:
        olt.sync_requested, olt.status, olt.status_msg = True, "pending", "Connection changed; syncing again…"
        await zte.links.drop(olt.id)
        zte._IFMAP.pop(olt.id, None)
    await s.commit()
    await bus.publish(sync.olt_event(olt))
    return olt_dict(olt, (await sync.counts_for(s, olt_id)).get(olt_id))


@router.delete("/olts/{olt_id}")
async def delete_olt(olt_id: int, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    await s.delete(olt)
    await s.commit()
    await zte.links.drop(olt_id)
    await bus.publish({"type": "olt_deleted", "id": olt_id})
    return {"ok": True}


@router.post("/olts/{olt_id}/sync", status_code=202)
async def request_sync(olt_id: int, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    olt.sync_requested = True
    await s.commit()
    return {"ok": True, "message": "Sync scheduled"}


@router.post("/olts/{olt_id}/save-config")
async def save_config(olt_id: int, s: AsyncSession = Depends(get_session)):
    olt = await _get_olt(s, olt_id)
    if olt.vendor != "zte":
        raise HTTPException(409, f"Config saves are not supported for {olt.vendor.upper()} OLTs.")
    ok, msg = await sync.do_save(olt_id)
    return {"ok": ok, "message": msg}


# ───────────────────────── tab Configure: cek per ONU ─────────────────────────
async def _onu_ctx(s: AsyncSession, onu_id: int):
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)
    if olt and olt.vendor != "zte":
        raise HTTPException(409, f"Live configuration reads are not supported for {olt.vendor.upper()} OLTs.")
    try:
        conn = zte.conn_from_row(olt)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return onu, olt, conn, (onu.shelf, onu.slot, onu.pon, onu.onu_number)


async def _guard(coro):
    try:
        return await coro
    except BusyError as exc:
        raise HTTPException(503, str(exc))
    except asyncio.TimeoutError:
        raise HTTPException(504, "OLT did not respond (timeout)")
    except (TelnetError, SnmpError) as exc:
        raise HTTPException(502, str(exc))
    except OSError as exc:
        raise HTTPException(502, f"Connection to OLT failed: {exc}")


@router.get("/onus/{onu_id}")
async def get_onu(onu_id: int, s: AsyncSession = Depends(get_session)):
    onu = await s.get(ONU, onu_id)
    if not onu or onu.hidden_duplicate:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)
    iface = (f"EPON 0/{onu.pon}:{onu.onu_number}" if olt.vendor == "hioso" else
             f"EPON 0/{onu.slot}/{onu.pon} ONU {onu.onu_number}" if olt.vendor == "cdata" else
             zte.profile_for(olt.cli_family).onu_if.format(
                 shelf=onu.shelf, slot=onu.slot, pon=onu.pon, onu=onu.onu_number))
    return {**onu_dict(onu, olt.name), "interface": iface,
            "olt": {"id": olt.id, "name": olt.name, "ip": olt.ip, "model": olt.model,
                    "vendor": olt.vendor, "cli_family": olt.cli_family,
                    "rx_warn": olt.rx_warn, "rx_crit": olt.rx_crit,
                    "write_enabled": bool(olt.write_enabled),
                    "telnet_configured": bool(olt.telnet_user and olt.telnet_pass_enc)},
            "zone_id": onu.zone_id, "odb_id": onu.odb_id, "address": onu.address or "", "contact": onu.contact or "",
            "external_id": onu.external_id or "", "mode": onu.mode or "", "auth_date": sync.iso(onu.auth_date),
            "dl_profile_id": onu.dl_profile_id, "ul_profile_id": onu.ul_profile_id, "distance_m": onu.distance_m,
            "mgmt_ip": onu.mgmt_ip or "", "mgmt_vlan": onu.mgmt_vlan}


@router.get("/onus/{onu_id}/status")
async def onu_status(onu_id: int, summary: bool = Query(False), s: AsyncSession = Depends(get_session)):
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)
    if olt and olt.vendor == "hioso":
        try:
            data = await hioso.read_onus(hioso.client_from_row(olt))
        except SnmpError as exc:
            raise HTTPException(502, f"HIOSO SNMP read failed: {exc}")
        item = data.get((onu.pon, onu.onu_number), {})
        state = item.get("state", "")
        status = "online" if state == "1" else "offline" if state == "2" else "unknown"
        try:
            rx_power = float(item.get("rx", ""))
            if item.get("rx", "").casefold() == "na" or rx_power == 0:
                rx_power = None
        except ValueError:
            rx_power = None
        now = utcnow()
        if onu.status != status or onu.phase_state != state:
            onu.status, onu.phase_state, onu.status_changed_at = status, state, now
        onu.rx_power, onu.updated_at = rx_power, now
        await s.commit()
        fields = {}
        detail_error = ""
        if olt.telnet_user and olt.telnet_pass_enc:
            try:
                fields = await hioso.read_onu_detail(olt, onu.pon, onu.onu_number)
            except (TelnetError, asyncio.TimeoutError, OSError, ValueError) as exc:
                detail_error = str(exc)
        else:
            detail_error = "HIOSO Telnet credentials are not configured; showing SNMP status only."
        return {"status": status, "phase_state": state, "optical": {"onu_rx": rx_power, "olt_rx": None},
                "interface": f"EPON 0/{onu.pon}:{onu.onu_number}", "fields": fields,
                "error": detail_error}
    if olt and olt.vendor == "cdata":
        try:
            data = await cdata.read_onus(cdata.client_from_row(olt))
        except SnmpError as exc:
            raise HTTPException(502, f"C-Data SNMP read failed: {exc}")
        item = data.get((onu.slot, onu.pon, onu.onu_number), {})
        state = item.get("state", "")
        status = "online" if state == "1" else "offline" if state == "2" else "unknown"
        try:
            rx_power = float(item.get("rx", ""))
        except ValueError:
            rx_power = None
        if rx_power == 0:
            rx_power = None
        now = utcnow()
        if onu.status != status or onu.phase_state != state:
            onu.status, onu.phase_state, onu.status_changed_at = status, state, now
        onu.rx_power, onu.updated_at = rx_power, now
        await s.commit()
        return {"status": status, "phase_state": state,
                "optical": {"onu_rx": rx_power, "olt_rx": None},
                "interface": f"EPON 0/{onu.slot}/{onu.pon} ONU {onu.onu_number}",
                "fields": {"ONU MAC": item.get("mac", "").upper(), "ONU index": f"{onu.slot}/{onu.pon}:{onu.onu_number}"},
                "error": ""}
    onu, olt, conn, key = await _onu_ctx(s, onu_id)
    res = await _guard(zte.onu_status(conn, key, include_mac=not summary))
    optical, changed = res["optical"], False
    res["status"] = sync.effective_status(res["status"], (optical.get("onu_rx"), optical.get("olt_rx")))
    for k, v in res["fields"].items():          # Distance from detail-info (e.g. "ONU Distance: 3540m").
        if "distance" in k.lower() and (m := re.search(r"\d+", v)) and onu.distance_m != int(m.group()):
            onu.distance_m, changed = int(m.group()), True
    if onu.auth_date is None and res.get("history"):   # tanggal otorisasi = authpass pertama di riwayat ONU
        try:
            onu.auth_date, changed = datetime.strptime(res["history"][0]["authpass"], "%Y-%m-%d %H:%M:%S"), True
        except ValueError:
            pass
    live = res["status"] != "unknown" and not res["error"]
    mac = res.get("mac", {})
    if mac.get("ok"):
        changed = await sync.store_onu_macs(s, onu.id, mac["rows"]) or changed
    if live:
        now = utcnow()
        if onu.status != res["status"] or onu.phase_state != res["phase_state"]:
            onu.status, onu.phase_state, onu.status_changed_at = res["status"], res["phase_state"], now
            changed = True
        for col, val in (("rx_power", optical.get("onu_rx")), ("olt_rx_power", optical.get("olt_rx"))):
            if val is not None and getattr(onu, col) != val:
                setattr(onu, col, val)
                changed = True
    if changed:
        onu.updated_at = utcnow()
        await s.commit()
        if live:
            await bus.publish({"type": "onus", "olt_id": onu.olt_id, "changes": [
                {"id": onu.id, "status": onu.status, "phase_state": onu.phase_state,
                 "rx_power": onu.rx_power, "olt_rx_power": onu.olt_rx_power}]})
            await bus.publish({"type": "counts", "olt_id": onu.olt_id,
                               "counts": (await sync.counts_for(s, onu.olt_id)).get(onu.olt_id)})
    return res


@router.get("/onus/{onu_id}/mac")
async def onu_mac(onu_id: int, s: AsyncSession = Depends(get_session)):
    onu, _, conn, key = await _onu_ctx(s, onu_id)
    mac = await _guard(zte.read_onu_mac(conn, key))
    if mac["ok"] and await sync.store_onu_macs(s, onu.id, mac["rows"]):
        onu.updated_at = utcnow()
        await s.commit()
    return mac


@router.get("/onus/{onu_id}/running-config")
async def onu_running_config(onu_id: int, s: AsyncSession = Depends(get_session)):
    _, _, conn, key = await _onu_ctx(s, onu_id)
    return await _guard(zte.onu_text(conn, key, "config"))


@router.get("/onus/{onu_id}/sw-info")
async def onu_sw_info(onu_id: int, s: AsyncSession = Depends(get_session)):
    _, _, conn, key = await _onu_ctx(s, onu_id)
    return await _guard(zte.onu_text(conn, key, "sw"))


@router.get("/onus/{onu_id}/traffic")
async def onu_traffic(onu_id: int, s: AsyncSession = Depends(get_session)):
    _, _, conn, key = await _onu_ctx(s, onu_id)
    return await _guard(zte.onu_traffic(conn, key))


# ───────────────────────── WebSocket realtime ─────────────────────────
@ws_router.websocket("/ws")
async def websocket_events(ws: WebSocket):
    user_id = ws.session.get("user_id")
    if not user_id:
        await ws.close(code=4401)
        return
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
    if not user:
        await ws.close(code=4401)
        return
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc != ws.headers.get("host"):
        await ws.close(code=4403)
        return
    role = user.role
    await ws.accept()
    await ws.send_json({"type": "hello"})
    async with bus.subscribe() as q:
        recv = asyncio.create_task(ws.receive())
        get = None
        try:
            while True:
                get = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({get, recv}, timeout=25, return_when=asyncio.FIRST_COMPLETED)
                if recv in done:
                    if recv.result().get("type") == "websocket.disconnect":
                        break
                    recv = asyncio.create_task(ws.receive())
                if get in done:
                    async with SessionLocal() as session:
                        user = await session.get(User, user_id)
                    if not user:
                        await ws.close(code=4401)
                        break
                    role = user.role
                    event = get.result()
                    if role == "olt_limited":
                        async with SessionLocal() as session:
                            allowed = await user_olt_ids(session, user.id)
                        if event.get("type") in {"counts", "onus", "onus_reload"} and event.get("olt_id") in allowed:
                            await ws.send_json(event)
                    elif role != "read_only" or event.get("type") in {"counts", "onus", "onus_reload"}:
                        await ws.send_json(event)
                elif not done:
                    get.cancel()
                    await ws.send_json({"type": "ping"})
        except Exception:
            pass
        finally:
            recv.cancel()
            if get and not get.done():
                get.cancel()
