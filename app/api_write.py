"""OLT changes (preview -> execution), auditing, and additional reads (ONU resync, Ethernet ports, diagnostics)."""
import asyncio
import logging
import re
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import _guard
from app.bus import bus
from app.db import get_session
from app.drivers import zte, zte_parsers as P
from app.models import AuditLog, OLT, ONU, OnuMacAddress, OnuType, SpeedProfile, utcnow
from app.security import require_admin, require_user
from app.services import onu_moves, provision as V, sync

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])
log = logging.getLogger("smartolt.api_write")
Action = Literal["register_onu", "delete_onu", "reboot_onu", "restore_onu", "disable_onu", "enable_onu",
                 "eth_state", "eth_config", "onu_mode", "pppoe_credentials", "apply_speed", "rename_onu",
                 "vlan_add", "uplink_vlan", "push_speed"]


class WriteIn(BaseModel):
    action: Action
    olt_id: Optional[int] = None
    onu_id: Optional[int] = None
    params: dict = Field(default_factory=dict)


class ExecIn(WriteIn):
    commands: Optional[list[str]] = Field(None, max_length=80)     # Commands edited in the preview
    confirm: bool = False
    save_after: bool = False


class ReplaceSnIn(BaseModel):
    sn: str = Field(min_length=8, max_length=16)
    confirm: bool = False


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
    """Apply successful changes to the database; regular polling refreshes live ONU status."""
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
    elif b.action == "pppoe_credentials" and onu:
        onu.pppoe_user = plan.meta["user"]
    elif b.action == "apply_speed" and onu:
        onu.dl_profile_id, onu.ul_profile_id = p.get("dl_profile_id") or None, p.get("ul_profile_id") or None
    elif b.action == "rename_onu" and onu:
        onu.name = onu.cfg_name = onu.description = str(p["name"]).strip()
    olt = await s.get(OLT, olt.id)
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


@router.post("/onus/{onu_id}/replace-sn")
async def replace_onu_sn(onu_id: int, body: ReplaceSnIn, user: str = Depends(require_admin),
                        s: AsyncSession = Depends(get_session)):
    """Replace the physical ONU at its current OLT/PON while preserving its validated bridge service."""
    if not body.confirm:
        raise HTTPException(422, "Confirmation is required.")
    onu, olt, conn, key = await _onu_conn(s, onu_id)
    if not olt.write_enabled:
        raise HTTPException(403, f"Changes to OLT '{olt.name}' are not enabled.")
    try:
        new_sn = V.serial(body.sn)
    except V.PlanError as exc:
        raise HTTPException(422, str(exc))
    old_sn = onu.sn.strip().upper()
    if new_sn == old_sn:
        raise HTTPException(422, "The new serial number is the same as the current ONU serial number.")
    duplicate = (await s.scalars(select(ONU.id).where(
        func.upper(func.trim(ONU.sn)) == new_sn, ONU.id != onu.id
    ).limit(1))).first()
    if duplicate:
        raise HTTPException(409, f"SN {new_sn} is already registered in the dashboard.")

    try:
        snapshot = await onu_moves.read_move_config(onu, olt)
    except V.PlanError as exc:
        raise HTTPException(422, str(exc))
    except (TelnetError, BusyError, OSError, ValueError, asyncio.TimeoutError) as exc:
        raise HTTPException(502, f"Could not validate the current ONU configuration: {exc}")

    try:
        unconfigured = await _guard(zte.scan_uncfg(conn))
    except HTTPException:
        raise
    candidates = [item for item in unconfigured["rows"] if item["sn"].strip().upper() == new_sn]
    at_location = [item for item in candidates
                   if (item["shelf"], item["slot"], item["pon"]) == key[:3]]
    if not unconfigured["ok"]:
        raise HTTPException(502, f"OLT rejected the unconfigured ONU scan: {unconfigured['raw'][:120]}")
    if len(candidates) != 1 or len(at_location) != 1:
        raise HTTPException(409, f"SN {new_sn} must be detected exactly once as unconfigured on this ONU's current OLT/PON.")

    profiles = (await s.scalars(select(SpeedProfile))).all()
    profile_ids = {(profile.name, profile.direction): profile.id for profile in profiles}
    ul_id = profile_ids.get((snapshot["ul_name"], "upload"))
    dl_id = profile_ids.get((snapshot["dl_name"], "download"))
    onu_type = (await s.scalars(select(OnuType).where(OnuType.name == snapshot["onu_type"]))).first()
    params = {
        **snapshot, "sn": new_sn, "slot": onu.slot, "pon": onu.pon,
        "ul_profile_id": ul_id, "dl_profile_id": dl_id,
        "zone_id": onu.zone_id, "odb_id": onu.odb_id, "address": onu.address,
        "contact": onu.contact, "external_id": new_sn,
    }
    used = set(await s.scalars(select(ONU.onu_number).where(
        ONU.olt_id == olt.id, ONU.slot == onu.slot, ONU.pon == onu.pon
    )))
    context = {
        "used": used, "ul_name": snapshot["ul_name"], "dl_name": snapshot["dl_name"],
        "eth_ports": onu_type.eth_ports if onu_type else 1, "type_unknown": onu_type is None,
    }
    try:
        register_plan = V.plan_register(olt, params, context)
    except V.PlanError as exc:
        raise HTTPException(422, f"Could not prepare the replacement configuration: {exc}")

    async def audit(action: str, ok: bool, commands: list[str], output: str):
        s.add(AuditLog(
            user=user, olt_id=olt.id, olt_name=olt.name, action=action,
            target=f"{old_sn} -> {new_sn} ({onu.slot}/{onu.pon})", ok=ok,
            commands="\n".join(V.mask(command) for command in commands),
            output=V.mask(output)[:8000],
        ))
        await s.commit()

    async def pre(cli):
        nonlocal register_plan
        await V.verify_speed_profiles(cli, olt, snapshot["ul_name"], snapshot["dl_name"])
        old_lookup = await cli.run(f"show gpon onu by sn {old_sn}", overall=20, idle=10)
        old_location = re.search(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", old_lookup.text, re.I)
        if old_lookup.error or not old_lookup.complete or not old_location or tuple(
                int(value) for value in old_location.groups()) != key:
            raise V.PlanError("The original ONU is no longer registered at its recorded interface.")
        new_lookup = await cli.run(f"show gpon onu by sn {new_sn}", overall=20, idle=10)
        if new_lookup.error or not new_lookup.complete:
            raise V.PlanError("Could not verify whether the replacement SN is already registered.")
        if re.search(r"gpon[-_]onu[-_]\d+/\d+/\d+:\d+", new_lookup.text, re.I):
            raise V.PlanError(f"Replacement SN {new_sn} is already registered on this OLT.")
        uncfg_result = await cli.run(conn.profile.uncfg_cmd, overall=60, idle=15)
        uncfg_rows = P.parse_uncfg(uncfg_result.text) if not uncfg_result.error else []
        if (uncfg_result.error or not uncfg_rows) and conn.profile.uncfg_cmd_alt:
            uncfg_result = await cli.run(conn.profile.uncfg_cmd_alt, overall=60, idle=15)
            uncfg_rows = P.parse_uncfg(uncfg_result.text) if not uncfg_result.error else []
        matching_new = [item for item in uncfg_rows if item["sn"].strip().upper() == new_sn
                        and (item["shelf"], item["slot"], item["pon"]) == key[:3]]
        if uncfg_result.error or len(matching_new) != 1:
            raise V.PlanError(f"Replacement SN {new_sn} is no longer uniquely detected unconfigured on this PON.")
        state = await cli.run(f"show gpon onu state {V._olt_if(olt, onu.slot, onu.pon)}", overall=60, idle=20)
        if state.error or not state.complete:
            raise V.PlanError("Could not verify available ONU indexes on the current PON.")
        live = {entry[3] for entry in P.parse_onu_state(state.text)}
        if register_plan.meta["number"] in live or register_plan.meta["number"] in used:
            context["used"] = used | live
            register_plan = V.plan_register(olt, params, context)
            return register_plan.commands, register_plan.rollback
        return None

    try:
        result = await _guard(V.run_plan(conn, register_plan.commands, register_plan.rollback, pre=pre))
    except (V.PlanError, HTTPException) as exc:
        await audit("replace_onu_sn_register_new", False, register_plan.commands, str(exc))
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(409, str(exc))
    await audit("replace_onu_sn_register_new", result.ok, result.replaced[0],
                "\n".join(f"{entry['cmd']} -> {entry['output']}" for entry in result.log))
    if not result.ok:
        raise HTTPException(409, result.message)

    new_number = register_plan.meta["number"]

    async def registered_locations(sn: str) -> list[tuple[int, int, int, int]]:
        async def query():
            async with zte.links.session(conn, wait=30, lane="check") as cli:
                return await cli.run(f"show gpon onu by sn {sn}", overall=20, idle=10)
        lookup = await _guard(query())
        if lookup.error or not lookup.complete:
            raise HTTPException(502, f"Could not verify SN {sn} registration on the OLT.")
        return [tuple(int(value) for value in match.groups()) for match in
                re.finditer(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", lookup.text, re.I)]

    expected_new_location = (onu.shelf, onu.slot, onu.pon, new_number)
    try:
        new_locations = await registered_locations(new_sn)
    except HTTPException:
        raise HTTPException(502, f"New ONU configuration was sent, but its registration could not be verified. Check {new_sn} and {old_sn} on the OLT.")
    if new_locations != [expected_new_location]:
        raise HTTPException(502, f"New SN {new_sn} was not verified at the expected interface. The original ONU configuration was left intact.")

    delete_plan = V.plan_delete(olt, onu, {}, {})
    new_onu = ONU(olt_id=olt.id, shelf=olt.shelf, slot=onu.slot, pon=onu.pon,
                  onu_number=new_number, sn=new_sn)

    async def cleanup_new_onu() -> str:
        cleanup = V.plan_delete(olt, new_onu, {}, {})
        try:
            cleanup_result = await _guard(V.run_plan(conn, cleanup.commands, cleanup.rollback))
        except HTTPException as exc:
            await audit("replace_onu_sn_cleanup_new", False, cleanup.commands, str(exc))
            return f"New ONU cleanup failed: {exc.detail}"
        await audit("replace_onu_sn_cleanup_new", cleanup_result.ok, cleanup.commands, cleanup_result.message)
        if not cleanup_result.ok:
            return f"New ONU cleanup failed: {cleanup_result.message}"
        try:
            remaining = await registered_locations(new_sn)
        except HTTPException as exc:
            return f"New ONU cleanup could not be verified: {exc.detail}"
        return "" if not remaining else f"New SN remains registered at {remaining}."

    try:
        removed = await _guard(V.run_plan(conn, delete_plan.commands, delete_plan.rollback))
    except HTTPException as exc:
        await audit("replace_onu_sn_remove_old", False, delete_plan.commands, str(exc))
        removed = None
        remove_message = exc.detail
    else:
        await audit("replace_onu_sn_remove_old", removed.ok, delete_plan.commands, removed.message)
        remove_message = removed.message
    try:
        old_locations = await registered_locations(old_sn)
    except HTTPException:
        raise HTTPException(502, f"New ONU is configured, but removal of old SN {old_sn} could not be verified. Check the OLT manually.")
    if old_locations:
        cleanup_message = await cleanup_new_onu()
        detail = f"Old SN {old_sn} remains at {old_locations}."
        if cleanup_message:
            detail += f" {cleanup_message}"
        raise HTTPException(502, detail)
    if removed is None or not removed.ok:
        log.warning("OLT reported a failed old ONU removal command although SN %s is verified absent", old_sn)
        log.warning("Old ONU removal result: %s", remove_message)

    onu.sn = new_sn
    onu.external_id = new_sn
    onu.onu_number = new_number
    onu.status = "unknown"
    onu.phase_state = ""
    onu.rx_power = None
    onu.olt_rx_power = None
    onu.status_changed_at = None
    onu.updated_at = utcnow()
    await s.execute(delete(OnuMacAddress).where(OnuMacAddress.onu_id == onu.id))
    olt.sync_requested = True
    try:
        await s.commit()
    except IntegrityError as exc:
        await s.rollback()
        await audit("replace_onu_sn_database_sync_failed", False, [],
                    f"OLT replacement succeeded, but dashboard update failed: {exc}")
        raise HTTPException(500, "OLT replacement succeeded, but the dashboard update failed. Check the ONU record and OLT configuration manually.")
    await bus.publish({"type": "onus_reload", "olt_id": olt.id})
    return {"ok": True, "old_sn": old_sn, "sn": new_sn, "external_id": new_sn,
            "location": f"{onu.slot}/{onu.pon}:{onu.onu_number}"}


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
