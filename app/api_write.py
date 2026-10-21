"""OLT changes (preview -> execution), auditing, and additional reads (ONU resync, Ethernet ports, diagnostics)."""
import re
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import _guard
from app.bus import bus
from app.db import get_session
from app.drivers import zte, zte_parsers as P
from app.models import AuditLog, OLT, ONU, OnuType, SpeedProfile, utcnow
from app.security import require_user
from app.services import provision as V, sync

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])
Action = Literal["register_onu", "delete_onu", "reboot_onu", "restore_onu", "disable_onu", "enable_onu",
                 "eth_state", "eth_config", "onu_mode", "apply_speed", "rename_onu", "vlan_add", "uplink_vlan", "push_speed"]


class WriteIn(BaseModel):
    action: Action
    olt_id: Optional[int] = None
    onu_id: Optional[int] = None
    params: dict = Field(default_factory=dict)


class ExecIn(WriteIn):
    commands: Optional[list[str]] = Field(None, max_length=80)     # Commands edited in the preview
    confirm: bool = False
    save_after: bool = False


async def _profile_name(s, pid, direction: str) -> Optional[str]:
    if not pid:
        return None
    p = await s.get(SpeedProfile, int(pid))
    if not p or p.direction != direction:
        raise V.PlanError(f"Invalid {direction} speed profile")
    return p.name


async def _build(s: AsyncSession, b: WriteIn):
    p = b.params or {}
    onu = None
    if b.action in V.ONU_ACTIONS:
        onu = await s.get(ONU, b.onu_id or 0)
        if not onu:
            raise HTTPException(404, "ONU not found")
        olt = await s.get(OLT, onu.olt_id)
    else:
        olt = await s.get(OLT, b.olt_id or 0)
        if not olt:
            raise HTTPException(404, "OLT not found")
    try:
        ctx: dict = {}
        if b.action == "register_onu":
            slot, pon = int(p.get("slot", 0)), int(p.get("pon", 0))
            ctx["used"] = set(await s.scalars(select(ONU.onu_number).where(ONU.olt_id == olt.id, ONU.slot == slot, ONU.pon == pon)))
            sn = str(p.get("sn", "")).strip().upper()
            dup = (await s.scalars(select(ONU).where(ONU.olt_id == olt.id, ONU.sn == sn))).first() if sn else None
            ctx["sn_exists"] = f"{dup.slot}/{dup.pon}:{dup.onu_number}" if dup else ""
            ctx["ul_name"] = await _profile_name(s, p.get("ul_profile_id"), "upload")
            ctx["dl_name"] = await _profile_name(s, p.get("dl_profile_id"), "download")
            t = (await s.scalars(select(OnuType).where(OnuType.name == str(p.get("onu_type", ""))))).first()
            ctx["eth_ports"], ctx["type_unknown"] = (t.eth_ports if t else 1), t is None
            plan = V.plan_register(olt, p, ctx)
        elif b.action in V.ONU_ACTIONS:
            t = (await s.scalars(select(OnuType).where(OnuType.name == onu.onu_type))).first()
            ctx["eth_ports"], ctx["eth_assumed"] = (t.eth_ports if t and t.eth_ports else 4), not (t and t.eth_ports)
            if b.action == "apply_speed":
                ctx["ul_name"] = await _profile_name(s, p.get("ul_profile_id"), "upload")
                ctx["dl_name"] = await _profile_name(s, p.get("dl_profile_id"), "download")
            plan = V.ONU_ACTIONS[b.action](olt, onu, p, ctx)
        else:
            if b.action == "push_speed":
                sp = await s.get(SpeedProfile, int(p.get("profile_id") or 0))
                ctx["profile"] = {"name": sp.name, "kbps": sp.kbps, "direction": sp.direction} if sp else None
            plan = V.OLT_ACTIONS[b.action](olt, None, p, ctx)
    except V.PlanError as exc:
        raise HTTPException(422, str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"Invalid parameter: {exc}")
    return plan, olt, onu, ctx


@router.post("/plan")
async def plan_preview(b: WriteIn, s: AsyncSession = Depends(get_session)):
    plan, olt, _, _ = await _build(s, b)
    d = plan.public()
    d["olt"] = {"id": olt.id, "name": olt.name, "write_enabled": bool(olt.write_enabled)}
    return d


async def _effects(s: AsyncSession, b: WriteIn, plan: V.Plan, olt: OLT, onu: Optional[ONU], number: Optional[int]) -> Optional[int]:
    """Apply successful changes to the dashboard database and request a sync to refresh OLT data."""
    p, new_id = b.params or {}, None
    if b.action == "register_onu":
        m = plan.meta
        o = ONU(olt_id=olt.id, shelf=olt.shelf, slot=m["slot"], pon=m["pon"], onu_number=number or m["number"], sn=m["sn"],
                onu_type=str(p.get("onu_type", "")), name=m["name"], cfg_name=m["name"], description=m["name"], vlan=m["vlan"],
                mode=m["mode"], conn_type="pppoe" if m["mode"] == "routing" else "", pppoe_user=p.get("pppoe_user", "") if m["mode"] == "routing" else "",
                status="unknown", zone_id=p.get("zone_id") or None, odb_id=p.get("odb_id") or None,
                address=V.text(p.get("address"), "Address", 300), contact=V.text(p.get("contact"), "Contact", 100),
                external_id=V.text(p.get("external_id"), "External ID", 64), auth_date=utcnow(), updated_at=utcnow(),
                dl_profile_id=p.get("dl_profile_id") or None, ul_profile_id=p.get("ul_profile_id") or None)
        s.add(o)
        try:
            await s.flush()
            new_id = o.id
        except IntegrityError:
            await s.rollback()
            olt = await s.get(OLT, olt.id)
    elif b.action == "delete_onu" and onu:
        await s.delete(onu)
    elif b.action == "onu_mode" and onu:
        m = plan.meta
        onu.vlan, onu.mode = m["vlan"], m["mode"]
        onu.conn_type, onu.pppoe_user = ("pppoe", m["user"]) if m["mode"] == "routing" else ("", "")
    elif b.action == "apply_speed" and onu:
        onu.dl_profile_id, onu.ul_profile_id = p.get("dl_profile_id") or None, p.get("ul_profile_id") or None
    elif b.action == "rename_onu" and onu:
        onu.name = onu.cfg_name = str(p["name"]).strip()
    olt = await s.get(OLT, olt.id)
    if olt:
        olt.sync_requested = True
    await s.commit()
    await bus.publish({"type": "onus_reload", "olt_id": olt.id if olt else None})
    return new_id


@router.post("/exec")
async def execute(b: ExecIn, user: str = Depends(require_user), s: AsyncSession = Depends(get_session)):
    plan, olt, onu, ctx = await _build(s, b)
    if not olt.write_enabled:
        raise HTTPException(403, f"Changes to OLT '{olt.name}' are not enabled. Enable 'Allow changes to OLT' in Settings → OLTs → Edit.")
    if not b.confirm:
        raise HTTPException(422, "Confirmation is required (confirm=true)")
    custom = b.commands is not None
    try:
        commands = V.validate_commands(b.commands) if custom else plan.commands
    except V.PlanError as exc:
        raise HTTPException(422, str(exc))
    rollback, final_number = plan.rollback, plan.meta.get("number")
    conn = zte.conn_from_row(olt)

    async def pre(cli):                        # Read-only check before changing the configuration.
        nonlocal final_number
        if b.action == "apply_speed":
            if not custom:
                await V.verify_speed_profiles(cli, olt, ctx.get("ul_name"), ctx.get("dl_name"))
            return None
        if b.action != "register_onu":
            return None
        if not custom:
            await V.verify_speed_profiles(cli, olt, ctx.get("ul_name"), ctx.get("dl_name"))
        sn, o_if = plan.meta["sn"], V._olt_if(olt, plan.meta["slot"], plan.meta["pon"])
        r = await cli.run(f"show gpon onu by sn {sn}", overall=20, idle=10)
        hit = re.search(r"gpon[-_]onu[-_]\d+/\d+/\d+:\d+", r.text)
        if hit:
            raise V.PlanError(f"SN {sn} is already registered on the OLT ({hit.group()})")
        r = await cli.run(f"show gpon onu state {o_if}", overall=60, idle=20)
        live = {k[3] for k in P.parse_onu_state(r.text)} if not r.error else set()
        if plan.meta["number"] in live:
            if custom:
                raise V.PlanError("ONU number is already in use on the OLT; create a new preview")
            ctx["used"] = ctx["used"] | live
            new = V.plan_register(olt, b.params, ctx)
            final_number = new.meta["number"]
            return new.commands, new.rollback
        return None

    try:
        res = await _guard(V.run_plan(conn, commands, rollback, pre=pre if b.action in ("register_onu", "apply_speed") else None))
    except V.PlanError as exc:
        await _audit(s, user, olt, b, plan, False, commands, str(exc), custom)
        raise HTTPException(409, str(exc))
    await _audit(s, user, olt, b, plan, res.ok, res.replaced[0], "\n".join(
        f"{'[rollback] ' if x['rollback'] else ''}{x['cmd']} -> {x['output']}" for x in res.log), custom)
    out = {"ok": res.ok, "message": res.message, "log": res.log, "rolled_back": res.rolled_back, "onu_id": None, "saved": None}
    if res.ok:
        out["onu_id"] = await _effects(s, b, plan, olt, onu, final_number)
        if b.save_after:
            ok, msg = await sync.do_save(olt.id)
            out["saved"] = {"ok": ok, "message": msg}
    return out


async def _audit(s, user, olt, b, plan, ok, commands, output, custom):
    s.add(AuditLog(user=user, olt_id=olt.id, olt_name=olt.name, action=b.action + (" (kustom)" if custom else ""),
                   target=plan.target, ok=ok, commands="\n".join(V.mask(c) for c in commands), output=V.mask(output)[:8000]))
    await s.commit()


@router.get("/audit")
async def audit_list(limit: int = Query(100, ge=1, le=500), olt_id: Optional[int] = None, s: AsyncSession = Depends(get_session)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if olt_id:
        q = q.where(AuditLog.olt_id == olt_id)
    return [{"id": a.id, "ts": sync.iso(a.ts), "user": a.user, "olt": a.olt_name, "action": a.action, "target": a.target,
             "ok": a.ok, "commands": a.commands, "output": a.output} for a in (await s.scalars(q)).all()]


# ───────────────────────── additional reads ─────────────────────────
async def _onu_conn(s: AsyncSession, onu_id: int):
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)
    try:
        return onu, olt, zte.conn_from_row(olt), (onu.shelf, onu.slot, onu.pon, onu.onu_number)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/onus/{onu_id}/resync")
async def resync_onu(onu_id: int, s: AsyncSession = Depends(get_session)):
    """'Resync config': read this ONU's configuration from the OLT into the dashboard (no OLT writes)."""
    onu, olt, conn, key = await _onu_conn(s, onu_id)
    text = await _guard(zte.read_onu_cfg(conn, key))
    items = [i for i in P.parse_running_config(text) if (i["shelf"], i["slot"], i["pon"], i["onu_number"]) == key]
    if not items:
        raise HTTPException(502, "Could not read ONU configuration from the OLT (check the 'Show running-config' output)")
    prof_id = {(p.name, p.direction): p.id for p in await s.scalars(select(SpeedProfile))}
    sync.apply_item(onu, items[0], prof_id, partial=True)
    onu.updated_at = utcnow()
    await s.commit()
    return {"ok": True, "vlan": onu.vlan, "name": onu.name, "mode": onu.mode, "conn_type": onu.conn_type, "mgmt_ip": onu.mgmt_ip}


@router.get("/onus/{onu_id}/eth")
async def onu_eth(onu_id: int, s: AsyncSession = Depends(get_session)):
    _, _, conn, key = await _onu_conn(s, onu_id)
    return await _guard(zte.read_eth(conn, key))


@router.get("/olts/{olt_id}/diag")
async def olt_diag(olt_id: int, s: AsyncSession = Depends(get_session)):
    olt = await s.get(OLT, olt_id)
    if not olt:
        raise HTTPException(404, "OLT not found")
    rows = await _guard(zte.read_diag(zte.conn_from_row(olt)))
    sysinfo = P.parse_system_group(next((r["output"] for r in rows if "system-group" in r["command"]), ""))
    return {"rows": rows, "uptime_s": sysinfo["uptime_s"]}
