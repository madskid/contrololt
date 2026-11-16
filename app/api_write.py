"""OLT changes (preview -> execution), auditing, and additional reads (ONU resync, Ethernet ports, diagnostics)."""
import asyncio
import logging
import re
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import _guard
from app.bus import bus
from app.db import SessionLocal, get_session
from app.drivers import hioso, zte, zte_parsers as P
from app.drivers.telnet import TelnetError
from app.models import AuditLog, DeleteOnuJob, OLT, ONU, OnuMacAddress, OnuType, OnuWanIp, ReplaceSnJob, SpeedProfile, utcnow
from app.security import require_admin, require_user
from app.services import onu_moves, provision as V, sync

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])
log = logging.getLogger("smartolt.api_write")
Action = Literal["register_onu", "delete_onu", "reboot_onu", "repush_onu", "restore_onu", "disable_onu", "enable_onu",
                 "eth_state", "eth_config", "onu_mode", "pppoe_credentials", "apply_speed", "rename_onu",
                 "vlan_add", "uplink_vlan", "push_speed", "convert_tcont"]


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
    sn: str = Field(min_length=8, max_length=500)
    confirm: bool = False


class DeleteOnuIn(BaseModel):
    confirm: bool = False


class HiosoRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    confirm: bool = False


class HiosoActionIn(BaseModel):
    confirm: bool = False


class RebootOnuIn(BaseModel):
    confirm: bool = False


def _replacement_serial(value: str) -> str:
    pasted = value.strip()
    rows = P.parse_uncfg(pasted)
    if rows:
        if len(rows) != 1:
            raise V.PlanError("Paste one ONU serial number or one row from Unconfigured.")
        return V.serial(rows[0]["sn"])
    match = re.fullmatch(r"(?:SN\s*[:=]\s*)?([A-Za-z0-9]{8,16})", pasted, re.I)
    if not match:
        raise V.PlanError("Enter one ONU serial number or paste one row from Unconfigured.")
    return V.serial(match.group(1))


def _olt_sn_not_found(output: str) -> bool:
    return bool(re.search(
        r"\b(?:not found|does not exist|doesn't exist|not exist|no such (?:onu|serial|sn)|"
        r"no (?:matching|related) (?:onu|serial|sn)|cannot find (?:onu|serial|sn))\b",
        output, re.I,
    ) or re.search(r"\bno related information to show[.!]?\s*$", output, re.I))


def _replacement_service_commands(outputs: list[str], conn, key: tuple[int, int, int, int]) -> list[str]:
    names = zte.onu_command_format(conn, key)
    headers = [f"interface {names['onu_if']}"]
    if names["vport_if"]:
        headers.append(f"interface {names['vport_if']}")
    headers.append(f"pon-onu-mng {names['onu_if']}")
    normalized = {header.casefold(): header for header in headers}
    sections = {header: [] for header in headers}
    seen = set()

    for output in outputs:
        active = None
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line == "!" or line.lower() == "end":
                active = None
                continue
            if line.casefold() in normalized:
                active = normalized[line.casefold()]
                seen.add(active)
                continue
            if line.lower().startswith(("interface ", "pon-onu-mng ")):
                active = None
                continue
            if active:
                sections[active].append(line)

    missing = [header for header in headers if header not in seen]
    if missing:
        raise V.PlanError(
            "The OLT did not return every ONU configuration section; "
            f"cannot safely replace the SN (missing: {', '.join(missing)})."
        )
    commands = []
    for header in headers:
        section = V.validate_commands([header, *sections[header], "exit"])
        commands.extend(section)
    return commands


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
    if olt.vendor != "zte":
        raise HTTPException(409, f"Dashboard write actions are not supported for {olt.vendor.upper()} OLTs.")
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
    plan, olt, onu, _ = await _build(s, b)
    if plan.meta.get("routing_vlan_only"):
        if onu is None:
            raise HTTPException(404, "ONU not found")
        try:
            plan = await _guard(_live_routing_vlan_plan(olt, onu, plan.meta["vlan"]))
        except V.PlanError as exc:
            raise HTTPException(409, str(exc))
    d = plan.public()
    d["olt"] = {"id": olt.id, "name": olt.name, "write_enabled": bool(olt.write_enabled)}
    return d


async def _live_routing_vlan_plan(olt: OLT, onu: ONU, vlan: int, cli=None) -> V.Plan:
    """Read both live ONU config sections and build a VLAN-only routing plan."""
    conn = zte.conn_from_row(olt)
    prof = zte.profile_for(olt.cli_family)
    n_if = V._onu_if(olt, onu)
    commands = (prof.cfg_cmds[0].format(onu_if=n_if, vport_if=""),
                prof.mng_cfg_cmd.format(onu_if=n_if))

    async def read_config(client):
        results = []
        for command in commands:
            result = await client.run(command, overall=30, idle=15)
            if result.error or not result.complete or not result.text.strip():
                raise V.PlanError(f"Could not read complete live ONU configuration ({command}); no changes were sent.")
            results.append(result.text)
        return V.plan_routing_vlan_update(olt, onu, vlan, results[0], results[1])

    if cli is not None:
        return await read_config(cli)
    async with zte.links.session(conn, wait=30, lane="check") as client:
        return await read_config(client)


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
        onu.name = onu.description = str(p["name"]).strip()
        onu.cfg_name = onu.name
        onu.name_locked = True
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
    routing_vlan_only = bool(plan.meta.get("routing_vlan_only"))
    if routing_vlan_only:
        if onu is None:
            raise HTTPException(404, "ONU not found")
        try:
            plan = await _guard(_live_routing_vlan_plan(olt, onu, plan.meta["vlan"]))
        except V.PlanError as exc:
            raise HTTPException(409, str(exc))
        if b.commands is not None:
            try:
                submitted = V.validate_commands(b.commands)
            except V.PlanError as exc:
                raise HTTPException(422, str(exc))
            if submitted != plan.commands:
                raise HTTPException(409, "The ONU configuration or preview changed. Reopen the preview before executing.")
        commands = plan.commands
        custom = False
    else:
        custom = b.commands is not None
    try:
        if not routing_vlan_only:
            commands = V.validate_commands(b.commands) if custom else plan.commands
    except V.PlanError as exc:
        raise HTTPException(422, str(exc))
    rollback, final_number = plan.rollback, plan.meta.get("number")
    conn = zte.conn_from_row(olt)

    async def pre(cli):                        # Read-only check before changing the configuration.
        nonlocal final_number
        if b.action == "convert_tcont":
            result = await cli.run("show running-config", overall=900, idle=45)
            if result.error or not result.complete or not result.text.strip():
                raise V.PlanError("Could not verify the current T-CONT definitions; no changes were sent.")
            current = [profile for profile in P.parse_tcont_profiles(result.text) if profile["type"] == 1]
            expected = b.params.get("profiles", [])
            signature = lambda profiles: sorted(
                (profile.get("mode", "gpon"), profile["name"], tuple(sorted(profile["parameters"].items())))
                for profile in profiles
            )
            if signature(current) != signature(expected):
                raise V.PlanError("T-CONT definitions changed since preview; generate a new preview.")
            return None
        if b.action == "apply_speed":
            if not custom:
                await V.verify_speed_profiles(cli, olt, ctx.get("ul_name"), ctx.get("dl_name"))
            return None
        if b.action == "onu_mode" and plan.meta.get("routing_vlan_only"):
            current = await _live_routing_vlan_plan(olt, onu, plan.meta["vlan"], cli)
            if current.commands != commands:
                raise V.PlanError("The ONU configuration changed since preview; no changes were sent. Generate a new preview.")
            return None
        if b.action == "onu_mode" and plan.meta["mode"] == "routing":
            mng_cmd = zte.profile_for(olt.cli_family).mng_cfg_cmd.format(onu_if=V._onu_if(olt, onu))
            result = await cli.run(mng_cmd, overall=30, idle=15)
            if result.error or not result.complete or not result.text.strip():
                raise V.PlanError("Could not read the ONU's current service configuration; no changes were sent.")
            cleanup = V.bridge_mode_cleanup(result.text)
            cleanup = [command for command in cleanup if command not in commands]
            if cleanup:
                updated = list(commands)
                try:
                    mng_index = next(i for i, command in enumerate(updated)
                                     if command.startswith("pon-onu-mng "))
                except StopIteration:
                    raise V.PlanError("The routing command list has no ONU management section; no changes were sent.")
                updated[mng_index + 1:mng_index + 1] = cleanup
                return updated, rollback
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
        res = await _guard(V.run_plan(
            conn, commands, rollback,
            pre=pre if b.action in ("register_onu", "apply_speed", "onu_mode", "convert_tcont") else None,
        ))
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
    if olt and olt.vendor != "zte":
        raise HTTPException(409, f"Live configuration reads are not supported for {olt.vendor.upper()} OLTs.")
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


async def _hioso_onu_context(onu_id: int, s: AsyncSession):
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)
    if not olt or olt.vendor != "hioso" or olt.model != "HA7308C":
        raise HTTPException(409, "This action is available only for HIOSO HA7308C ONUs.")
    if not olt.write_enabled:
        raise HTTPException(403, "Enable HIOSO ONU rename/reboot in the OLT settings first.")
    if not 1 <= onu.pon <= 8 or not 1 <= onu.onu_number <= 64:
        raise HTTPException(409, "The HIOSO EPON port/ONU index is outside the supported range.")
    return onu, olt


async def _audit_hioso_onu_action(s: AsyncSession, user: str, olt: OLT, onu: ONU,
                                  action: str, commands: list[str], output: str, ok: bool):
    s.add(AuditLog(user=user, olt_id=olt.id, olt_name=olt.name, action=action,
                   target=f"EPON 0/{onu.pon}:{onu.onu_number}", ok=ok,
                   commands="\n".join(commands), output=output[:8000]))
    await s.commit()


@router.post("/onus/{onu_id}/hioso/rename")
async def rename_hioso_onu(onu_id: int, body: HiosoRenameIn,
                           user: str = Depends(require_admin), s: AsyncSession = Depends(get_session)):
    if not body.confirm:
        raise HTTPException(422, "Confirmation is required.")
    onu, olt = await _hioso_onu_context(onu_id, s)
    try:
        name, commands = hioso.rename_commands(onu.pon, onu.onu_number, body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        outputs = await hioso.run_cli(olt, commands)
    except (TelnetError, asyncio.TimeoutError, OSError, ValueError) as exc:
        await _audit_hioso_onu_action(s, user, olt, onu, "hioso_rename_onu", commands, str(exc), False)
        raise HTTPException(502, f"HIOSO ONU rename failed: {exc}") from exc
    onu.name = onu.description = onu.cfg_name = name
    onu.name_locked = True
    onu.updated_at = utcnow()
    await _audit_hioso_onu_action(s, user, olt, onu, "hioso_rename_onu",
                                  commands, "\n".join(outputs), True)
    await bus.publish({"type": "onus_reload", "olt_id": olt.id})
    return {"ok": True, "name": name}


@router.post("/onus/{onu_id}/hioso/reboot")
async def reboot_hioso_onu(onu_id: int, body: HiosoActionIn,
                           user: str = Depends(require_admin), s: AsyncSession = Depends(get_session)):
    if not body.confirm:
        raise HTTPException(422, "Confirmation is required.")
    onu, olt = await _hioso_onu_context(onu_id, s)
    commands = hioso.reboot_commands(onu.pon, onu.onu_number)
    try:
        outputs = await hioso.run_cli(olt, commands)
    except (TelnetError, asyncio.TimeoutError, OSError, ValueError) as exc:
        await _audit_hioso_onu_action(s, user, olt, onu, "hioso_reboot_onu", commands, str(exc), False)
        raise HTTPException(502, f"HIOSO ONU reboot failed: {exc}") from exc
    await _audit_hioso_onu_action(s, user, olt, onu, "hioso_reboot_onu",
                                  commands, "\n".join(outputs), True)
    return {"ok": True, "message": "Reboot command accepted by the HIOSO OLT."}


@router.post("/onus/{onu_id}/reboot")
async def reboot_onu(onu_id: int, body: RebootOnuIn, user: str = Depends(require_user),
                     s: AsyncSession = Depends(get_session)):
    if not body.confirm:
        raise HTTPException(422, "Confirmation is required.")
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found.")
    olt = await s.get(OLT, onu.olt_id)
    if not olt:
        raise HTTPException(404, "OLT not found.")
    if olt.vendor == "hioso":
        onu, olt = await _hioso_onu_context(onu_id, s)
        commands = hioso.reboot_commands(onu.pon, onu.onu_number)
        try:
            outputs = await hioso.run_cli(olt, commands)
        except (TelnetError, asyncio.TimeoutError, OSError, ValueError) as exc:
            await _audit_hioso_onu_action(s, user, olt, onu, "hioso_reboot_onu", commands, str(exc), False)
            raise HTTPException(502, f"HIOSO ONU reboot failed: {exc}") from exc
        await _audit_hioso_onu_action(s, user, olt, onu, "hioso_reboot_onu",
                                      commands, "\n".join(outputs), True)
        return {"ok": True, "message": "Reboot command accepted by the HIOSO OLT."}
    if olt.vendor != "zte":
        raise HTTPException(409, f"ONU reboot is not supported for {olt.vendor.upper()} OLTs.")
    if not olt.write_enabled:
        raise HTTPException(403, f"Changes to OLT '{olt.name}' are not enabled.")
    request_body = WriteIn(action="reboot_onu", onu_id=onu_id)
    plan, olt, onu, _ = await _build(s, request_body)
    try:
        result = await _guard(V.run_plan(zte.conn_from_row(olt), plan.commands, plan.rollback))
    except V.PlanError as exc:
        await _audit(s, user, olt, request_body, plan, False, plan.commands, str(exc), False)
        raise HTTPException(409, str(exc)) from exc
    await _audit(s, user, olt, request_body, plan, result.ok, result.replaced[0],
                 "\n".join(f"{entry['cmd']} -> {entry['output']}" for entry in result.log), False)
    if not result.ok:
        raise HTTPException(502, result.message)
    return {"ok": True, "message": result.message or "Reboot command accepted by the ZTE OLT."}


@router.post("/onus/{onu_id}/replace-sn", status_code=202)
async def replace_onu_sn(onu_id: int, body: ReplaceSnIn, background_tasks: BackgroundTasks,
                        user: str = Depends(require_admin), s: AsyncSession = Depends(get_session)):
    if not body.confirm:
        raise HTTPException(422, "Confirmation is required.")
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)
    if not olt:
        raise HTTPException(404, "OLT not found")
    if olt.vendor != "zte":
        raise HTTPException(409, f"ONU replacement is not supported for {olt.vendor.upper()} OLTs.")
    if not olt.write_enabled:
        raise HTTPException(403, f"Changes to OLT '{olt.name}' are not enabled.")
    try:
        new_sn = _replacement_serial(body.sn)
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
    active_job = (await s.scalars(select(ReplaceSnJob.id).where(
        ReplaceSnJob.onu_id == onu.id, ReplaceSnJob.status.in_(("queued", "running"))
    ).limit(1))).first()
    if active_job:
        raise HTTPException(409, "A serial replacement is already queued or running for this ONU.")

    job = ReplaceSnJob(onu_id=onu.id, requested_by=user, old_sn=old_sn, new_sn=new_sn,
                       status="queued", message="Replacement is queued.")
    s.add(job)
    await s.commit()
    await s.refresh(job)
    background_tasks.add_task(_run_replace_sn_job, job.id)
    return {"id": job.id, "status": job.status, "old_sn": old_sn, "sn": new_sn}


@router.post("/onus/{onu_id}/delete", status_code=202)
async def delete_onu_background(onu_id: int, body: DeleteOnuIn, background_tasks: BackgroundTasks,
                               user: str = Depends(require_user), s: AsyncSession = Depends(get_session)):
    if not body.confirm:
        raise HTTPException(422, "Confirmation is required.")
    onu = await s.get(ONU, onu_id)
    if not onu:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)
    if not olt:
        raise HTTPException(404, "OLT not found")
    if olt.vendor != "zte":
        raise HTTPException(409, f"ONU deletion is not supported for {olt.vendor.upper()} OLTs.")
    if not olt.write_enabled:
        raise HTTPException(403, f"Changes to OLT '{olt.name}' are not enabled.")
    active_job = (await s.scalars(select(DeleteOnuJob.id).where(
        DeleteOnuJob.onu_id == onu.id, DeleteOnuJob.status.in_(("queued", "running"))
    ).limit(1))).first()
    if active_job:
        raise HTTPException(409, "Deletion is already queued or running for this ONU.")

    job = DeleteOnuJob(onu_id=onu.id, requested_by=user, status="queued", message="Deletion is queued.")
    s.add(job)
    await s.commit()
    await s.refresh(job)
    background_tasks.add_task(_run_delete_onu_job, job.id)
    return {"id": job.id, "status": job.status, "message": job.message}


@router.get("/onu-deletions/{job_id}")
async def delete_onu_job_status(job_id: int, user: str = Depends(require_user),
                               s: AsyncSession = Depends(get_session)):
    job = await s.get(DeleteOnuJob, job_id)
    if not job or job.requested_by != user:
        raise HTTPException(404, "ONU deletion job not found")
    return {"id": job.id, "status": job.status, "message": job.message}


async def _run_delete_onu_job(job_id: int):
    async with SessionLocal() as s:
        job = await s.get(DeleteOnuJob, job_id)
        if not job or job.status != "queued":
            return
        job.status, job.message, job.started_at = "running", "Deleting ONU from the OLT…", utcnow()
        onu_id, requested_by = job.onu_id, job.requested_by
        await s.commit()

    try:
        async with SessionLocal() as s:
            result = await execute(
                ExecIn(action="delete_onu", onu_id=onu_id, confirm=True), requested_by, s
            )
        status = "completed" if result["ok"] else "failed"
        message = result["message"] or ("ONU deletion completed." if result["ok"] else "ONU deletion failed.")
    except HTTPException as exc:
        status, message = "failed", str(exc.detail)
    except Exception:
        log.exception("ONU deletion background job %s failed unexpectedly", job_id)
        status, message = "failed", "Unexpected server error. Check server logs and verify the ONU state on the OLT."

    async with SessionLocal() as s:
        job = await s.get(DeleteOnuJob, job_id)
        if job:
            job.status, job.message, job.finished_at = status, message, utcnow()
            await s.commit()


@router.get("/onu-replacements/{job_id}")
async def replace_sn_job_status(job_id: int, _: str = Depends(require_admin),
                                s: AsyncSession = Depends(get_session)):
    job = await s.get(ReplaceSnJob, job_id)
    if not job:
        raise HTTPException(404, "Replace SN job not found")
    return {
        "id": job.id, "status": job.status, "old_sn": job.old_sn, "sn": job.new_sn,
        "message": job.message, "saved": job.saved, "save_message": job.save_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def _run_replace_sn_job(job_id: int):
    async with SessionLocal() as s:
        job = await s.get(ReplaceSnJob, job_id)
        if not job or job.status != "queued":
            return
        job.status, job.message, job.started_at = "running", "Replacing ONU SN on the OLT…", utcnow()
        await s.commit()
        onu_id, new_sn, requested_by = job.onu_id, job.new_sn, job.requested_by

    try:
        async with SessionLocal() as s:
            result = await _replace_onu_sn(
                onu_id, ReplaceSnIn(sn=new_sn, confirm=True), requested_by, s
            )
        async with SessionLocal() as s:
            job = await s.get(ReplaceSnJob, job_id)
            if job:
                job.status, job.message, job.saved = "completed", "ONU serial replacement completed.", result["saved"]
                job.save_message = result["save_message"]
                job.finished_at = utcnow()
                await s.commit()
    except HTTPException as exc:
        async with SessionLocal() as s:
            job = await s.get(ReplaceSnJob, job_id)
            if job:
                job.status, job.message, job.finished_at = "failed", str(exc.detail), utcnow()
                await s.commit()
    except Exception:
        log.exception("Replace SN background job %s failed unexpectedly", job_id)
        async with SessionLocal() as s:
            job = await s.get(ReplaceSnJob, job_id)
            if job:
                job.status = "failed"
                job.message = "Unexpected server error. Check server logs and verify the ONU state on the OLT."
                job.finished_at = utcnow()
                await s.commit()


async def _replace_onu_sn(onu_id: int, body: ReplaceSnIn, user: str, s: AsyncSession):
    """Replace only the ONU serial registration, leaving its service configuration untouched."""
    if not body.confirm:
        raise HTTPException(422, "Confirmation is required.")
    onu, olt, conn, key = await _onu_conn(s, onu_id)
    if not olt.write_enabled:
        raise HTTPException(403, f"Changes to OLT '{olt.name}' are not enabled.")
    try:
        new_sn = _replacement_serial(body.sn)
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

    olt_if = V._olt_if(olt, onu.slot, onu.pon)
    try:
        async with zte.links.fresh_session(conn, wait=60, lane="check") as cli:
            config = await cli.run(f"show running-config interface {olt_if}", overall=60, idle=20)
            service_outputs = []
            names = zte.onu_command_format(conn, key)
            for template in conn.profile.cfg_cmds:
                cmd = template.format(**names)
                service_output = await cli.run(cmd, overall=60, idle=20)
                if service_output.error or not service_output.complete:
                    raise HTTPException(
                        502, f"Could not safely capture ONU configuration with '{cmd}': "
                        f"{service_output.text.strip()[:200] or 'incomplete response'}"
                    )
                service_outputs.append(service_output.text)
            old_lookup = await cli.run(f"show gpon onu by sn {old_sn}", overall=30, idle=15)
            new_lookup = await cli.run(f"show gpon onu by sn {new_sn}", overall=30, idle=15)
    except HTTPException:
        raise
    except (TelnetError, BusyError, OSError, asyncio.TimeoutError) as exc:
        raise HTTPException(502, f"Could not read the ONU configuration from the OLT: {exc}")
    if config.error or not config.complete or not config.text.strip():
        raise HTTPException(502, f"Could not read the ONU's PON configuration from the OLT: {config.text.strip()[:160] or 'empty response'}")
    source = next((item for item in P.parse_running_config(config.text)
                   if (item["shelf"], item["slot"], item["pon"], item["onu_number"]) == key), None)
    if not source or source["sn"].strip().upper() != old_sn or not source["onu_type"]:
        raise HTTPException(409, "The ONU SN or type in the OLT running configuration does not match the dashboard.")
    old_location = re.search(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", old_lookup.text, re.I)
    if old_lookup.error or not old_lookup.complete or not old_location or tuple(
            int(value) for value in old_location.groups()) != key:
        raise HTTPException(409, "The original ONU is no longer registered at its recorded interface.")
    if not new_lookup.complete:
        raise HTTPException(
            409, f"Could not verify whether replacement SN {new_sn} is registered; "
            f"the OLT response was incomplete: {new_lookup.text.strip()[:160] or 'empty response'}."
        )
    if re.search(r"gpon[-_]onu[-_]\d+/\d+/\d+:\d+", new_lookup.text, re.I):
        raise HTTPException(409, f"Replacement SN {new_sn} is already registered on this OLT.")
    if new_lookup.error and not _olt_sn_not_found(new_lookup.text):
        raise HTTPException(
            409, f"Could not verify whether replacement SN {new_sn} is registered; "
            f"the OLT responded: {new_lookup.text.strip()[:160] or 'unknown error'}."
        )
    try:
        onu_type = V.ident(source["onu_type"], "ONU type")
        service_commands = _replacement_service_commands(service_outputs, conn, key)
    except V.PlanError as exc:
        raise HTTPException(422, str(exc))

    registration = f"onu {onu.onu_number} type {onu_type}"
    commands = V.validate_commands([
        f"interface {olt_if}",
        f"no onu {onu.onu_number}",
        f"{registration} sn {new_sn}",
        "exit",
        *service_commands,
    ])
    restore_commands = V.validate_commands([
        f"interface {olt_if}",
        f"{registration} sn {old_sn}",
        "exit",
        *service_commands,
    ])

    async def audit(action: str, ok: bool, commands: list[str], output: str):
        s.add(AuditLog(
            user=user, olt_id=olt.id, olt_name=olt.name, action=action,
            target=f"{old_sn} -> {new_sn} ({onu.slot}/{onu.pon})", ok=ok,
            commands="\n".join(V.mask(command) for command in commands),
            output=V.mask(output)[:8000],
        ))
        await s.commit()

    async def execute_config(commands_to_run: list[str]) -> tuple[bool, list[dict], str]:
        rows = []
        failure = ""
        async with zte.links.fresh_session(conn, wait=60, lane="config") as cli:
            for command in ["configure terminal", *commands_to_run]:
                result = await cli.run(command, overall=40, idle=20, confirm=True)
                row = {"cmd": V.mask(command), "output": V.mask(result.text)[:600], "error": result.error or not result.complete}
                rows.append(row)
                if row["error"]:
                    failure = f"Command rejected by OLT: {command}"
                    if result.text.strip():
                        failure += f". OLT response: {result.text.strip()[:300]}"
                    break
            try:
                await cli.run("end", overall=10, idle=5)
            except (TelnetError, asyncio.TimeoutError):
                if not failure:
                    raise
        return not failure, rows, failure

    async def restore_old() -> tuple[bool, str]:
        try:
            async with zte.links.fresh_session(conn, wait=60, lane="config") as cli:
                current = await cli.run(f"show running-config interface {olt_if}", overall=60, idle=20)
                if current.error or not current.complete:
                    return False, f"Could not inspect ONU registration before recovery: {current.text.strip()[:200]}"
                current_item = next((item for item in P.parse_running_config(current.text)
                                     if (item["shelf"], item["slot"], item["pon"], item["onu_number"]) == key), None)
                if current_item and current_item["sn"].strip().upper() == old_sn:
                    return True, "Original registration is still present."
                steps = ["configure terminal", f"interface {olt_if}"]
                if current_item:
                    steps.append(f"no onu {onu.onu_number}")
                steps += [f"{registration} sn {old_sn}", "exit", *service_commands, "end"]
                for command in steps:
                    result = await cli.run(command, overall=40, idle=20, confirm=True)
                    if result.error or not result.complete:
                        return False, (
                            f"Recovery command rejected: {command}. "
                            f"OLT response: {result.text.strip()[:300] or 'incomplete response'}"
                        )
                restored = await cli.run(f"show gpon onu by sn {old_sn}", overall=30, idle=15)
                found = re.search(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", restored.text, re.I)
                if restored.error or not restored.complete or not found or tuple(
                        int(value) for value in found.groups()) != key:
                    return False, "Previous SN could not be verified at its original ONU interface after recovery."
                saved = await cli.run(conn.profile.save_cmd, overall=180, idle=90, confirm=True)
                if saved.error or not saved.complete:
                    return False, f"Original ONU was restored, but saving it to OLT memory failed: {saved.text.strip()[:200]}"
        except (TelnetError, BusyError, OSError, asyncio.TimeoutError) as exc:
            return False, f"Recovery connection failed: {exc}"
        return True, "Original ONU registration and service configuration restored."

    try:
        ok, write_log, write_error = await execute_config(commands)
    except (TelnetError, BusyError, OSError, asyncio.TimeoutError) as exc:
        write_error = f"OLT connection failed during SN replacement: {exc}"
        restored, restore_message = await restore_old()
        await audit("replace_onu_sn_update", False, commands, write_error)
        await audit("replace_onu_sn_recovery", restored, restore_commands, restore_message)
        if restored:
            raise HTTPException(502, f"{write_error}. {restore_message}")
        raise HTTPException(
            502, f"{write_error}. Automatic recovery failed: {restore_message}. "
            f"Check ONU {onu.slot}/{onu.pon}:{onu.onu_number} on the OLT immediately."
        )
    await audit("replace_onu_sn_update", ok, commands, write_error or "\n".join(
        f"{entry['cmd']} -> {entry['output']}" for entry in write_log))
    if not ok:
        restored, restore_message = await restore_old()
        await audit("replace_onu_sn_recovery", restored, restore_commands, restore_message)
        if restored:
            raise HTTPException(409, f"{write_error}. {restore_message}")
        raise HTTPException(
            502, f"{write_error}. Automatic recovery failed: {restore_message}. "
            f"Check ONU {onu.slot}/{onu.pon}:{onu.onu_number} on the OLT immediately."
        )

    async def verify_replacement() -> tuple[list[tuple[int, int, int, int]], list[tuple[int, int, int, int]]]:
        async with zte.links.fresh_session(conn, wait=60, lane="check") as cli:
            async def registered_locations(sn: str) -> list[tuple[int, int, int, int]]:
                lookup = await cli.run(f"show gpon onu by sn {sn}", overall=30, idle=15)
                if not lookup.complete or (lookup.error and not _olt_sn_not_found(lookup.text)):
                    raise HTTPException(502, f"Could not verify SN {sn} registration on the OLT.")
                return [tuple(int(value) for value in match.groups()) for match in
                        re.finditer(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", lookup.text, re.I)]

            return await registered_locations(new_sn), await registered_locations(old_sn)

    try:
        new_locations, old_locations = await _guard(verify_replacement())
    except HTTPException as exc:
        restored, restore_message = await restore_old()
        await audit("replace_onu_sn_recovery", restored, restore_commands, restore_message)
        raise HTTPException(502, f"OLT verification failed: {exc.detail}. {restore_message}")
    expected_new_location = key
    if new_locations != [expected_new_location]:
        restored, restore_message = await restore_old()
        await audit("replace_onu_sn_recovery", restored, restore_commands, restore_message)
        raise HTTPException(502, f"SN {new_sn} was not verified at the original interface {key}. {restore_message}")
    if old_locations:
        restored, restore_message = await restore_old()
        await audit("replace_onu_sn_recovery", restored, restore_commands, restore_message)
        raise HTTPException(502, f"Old SN {old_sn} remains registered at {old_locations}. {restore_message}")

    onu.sn = new_sn
    onu.external_id = new_sn
    onu.status = "unknown"
    onu.phase_state = ""
    onu.rx_power = None
    onu.olt_rx_power = None
    onu.status_changed_at = None
    onu.updated_at = utcnow()
    await s.execute(delete(OnuMacAddress).where(OnuMacAddress.onu_id == onu.id))
    await s.execute(delete(OnuWanIp).where(OnuWanIp.onu_id == onu.id))
    olt.sync_requested = True
    try:
        async with zte.links.fresh_session(conn, wait=60, lane="config") as cli:
            save_result = await cli.run(conn.profile.save_cmd, overall=180, idle=90, confirm=True)
        saved = save_result.complete and not save_result.error
        save_message = save_result.text.strip()[:250] or ("OK" if saved else "OLT rejected the save command")
    except (TelnetError, BusyError, OSError, asyncio.TimeoutError) as exc:
        saved, save_message = False, str(exc)
    try:
        await s.commit()
    except IntegrityError as exc:
        await s.rollback()
        await audit("replace_onu_sn_database_sync_failed", False, [],
                    f"OLT replacement succeeded, but dashboard update failed: {exc}")
        raise HTTPException(500, "OLT replacement succeeded, but the dashboard update failed. Check the ONU record and OLT configuration manually.")
    await bus.publish({"type": "onus_reload", "olt_id": olt.id})
    return {"ok": True, "old_sn": old_sn, "sn": new_sn, "external_id": new_sn,
            "location": f"{onu.slot}/{onu.pon}:{onu.onu_number}",
            "saved": saved, "save_message": save_message}


@router.get("/onus/{onu_id}/eth")
async def onu_eth(onu_id: int, s: AsyncSession = Depends(get_session)):
    _, _, conn, key = await _onu_conn(s, onu_id)
    return await _guard(zte.read_eth(conn, key))


@router.get("/olts/{olt_id}/diag")
async def olt_diag(olt_id: int, s: AsyncSession = Depends(get_session)):
    olt = await s.get(OLT, olt_id)
    if not olt:
        raise HTTPException(404, "OLT not found")
    if olt.vendor != "zte":
        raise HTTPException(409, "CLI diagnostics are currently available only for ZTE OLTs.")
    rows = await _guard(zte.read_diag(zte.conn_from_row(olt)))
    sysinfo = P.parse_system_group(next((r["output"] for r in rows if "system-group" in r["command"]), ""))
    return {"rows": rows, "uptime_s": sysinfo["uptime_s"]}
