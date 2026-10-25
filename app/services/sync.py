"""OLT-to-database synchronization: full sync (running-config), fast polling (status and signal), and config saves."""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, func, update
from sqlalchemy.exc import IntegrityError

from app.bus import bus
from app.config import settings
from app.db import SessionLocal
from app.drivers import zte, zte_parsers as P
from app.drivers.snmp import SnmpError
from app.drivers.zte_parsers import phase_to_status
from app.models import AuditLog, OLT, ONU, OnuMacAddress, OltInfo, OnuType, SpeedProfile, utcnow
from app.services import provision as V

log = logging.getLogger("smartolt.sync")
STATUSES = ("online", "offline", "los", "power_fail", "unknown")


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def olt_event(olt: OLT) -> dict:
    return {"type": "olt", "id": olt.id, "status": olt.status, "msg": olt.status_msg,
            "poll_source": olt.poll_source, "last_poll_at": iso(olt.last_poll_at),
            "last_full_sync_at": iso(olt.last_full_sync_at), "last_save_at": iso(olt.last_save_at),
            "last_save_ok": olt.last_save_ok, "last_save_msg": olt.last_save_msg}


async def counts_for(session, olt_id: int | None = None) -> dict[int, dict]:
    q = select(ONU.olt_id, ONU.status, func.count()).group_by(ONU.olt_id, ONU.status)
    if olt_id:
        q = q.where(ONU.olt_id == olt_id)
    out: dict[int, dict] = {}
    for oid, st, n in (await session.execute(q)).all():
        c = out.setdefault(oid, {k: 0 for k in STATUSES})
        c[st] = c.get(st, 0) + n
    for c in out.values():
        c["total"] = sum(v for k, v in c.items() if k != "total")
    return out


async def _publish_counts(session, olt_id: int) -> None:
    c = (await counts_for(session, olt_id)).get(olt_id) or {**{k: 0 for k in STATUSES}, "total": 0}
    await bus.publish({"type": "counts", "olt_id": olt_id, "counts": c})


async def _mark(olt_id: int, status: str, msg: str) -> None:
    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if not olt:
            return
        olt.status, olt.status_msg = status, msg[:300]
        await s.commit()
        await bus.publish(olt_event(olt))


async def save_info(session, olt_id: int, kind: str, data) -> None:
    row = (await session.scalars(select(OltInfo).where(OltInfo.olt_id == olt_id, OltInfo.kind == kind))).first()
    payload = json.dumps(data)
    if row is None:
        session.add(OltInfo(olt_id=olt_id, kind=kind, data=payload, updated_at=utcnow()))
    else:
        row.data, row.updated_at = payload, utcnow()


async def load_info(session, olt_id: int, kind: str) -> tuple[dict, datetime | None]:
    row = (await session.scalars(select(OltInfo).where(OltInfo.olt_id == olt_id, OltInfo.kind == kind))).first()
    return (json.loads(row.data), row.updated_at) if row else ({}, None)


def parse_sw_version(descr: str) -> str:
    m = re.search(r"[Vv](\d+(?:\.\d+)+\w*)", descr or "")
    return m.group(1) if m else ""


async def import_masters(extras: dict) -> None:
    """Import existing OLT speed profiles and ONU types into the dashboard; add missing entries only."""
    async with SessionLocal() as s:
        have_sp = {(p.name, p.direction) for p in await s.scalars(select(SpeedProfile))}
        have_t = set(await s.scalars(select(OnuType.name)))
    todo: list = []
    for direction, items in extras.get("speed", {}).items():
        todo += [SpeedProfile(name=n[:60], direction=direction, kbps=k) for n, k in items.items() if (n, direction) not in have_sp]
    for name, t in extras.get("onu_types", {}).items():
        if name not in have_t:
            todo.append(OnuType(name=name[:60], eth_ports=t["eth"], wifi_ports=t["wifi"], voip_ports=t["pots"],
                                catv=1 if t["rf"] else 0, allow_custom=True))
    for row in todo:
        async with SessionLocal() as s:
            try:
                s.add(row)
                await s.commit()
            except IntegrityError:      # Already imported by another OLT sync.
                await s.rollback()


class _ProfileAlreadyPresent(Exception):
    pass


async def push_speed_profile_to_all_olts(profile_id: int, user: str) -> None:
    """Distribute a new profile to all OLTs; existing names are not changed."""
    async with SessionLocal() as s:
        profile = await s.get(SpeedProfile, profile_id)
        if not profile:
            log.warning("Speed profile distribution %s cancelled: profile no longer exists", profile_id)
            return
        profile_data = {"name": profile.name, "kbps": profile.kbps, "direction": profile.direction}
        targets = list((await s.scalars(select(OLT).order_by(OLT.name))).all())

    for olt in targets:
        commands: list[str] = []
        try:
            conn = zte.conn_from_row(olt)
            prof = zte.profile_for(olt.cli_family)
            list_cmd = prof.tcont_list_cmd if profile_data["direction"] == "upload" else prof.traffic_list_cmd

            async with zte.links.session(conn, lane="sync") as cli:
                listed = await cli.run(list_cmd, overall=30, idle=10)
            names = P.parse_profile_names(listed.text) if not listed.error and listed.complete else []
            if not names:
                raise V.PlanError(
                    f"Could not read the {profile_data['direction']} profile list; no changes were sent"
                )
            if profile_data["name"] in names:
                output = f"A profile with the same name already exists on the OLT; skipped without changes. Command: {list_cmd}"
                async with SessionLocal() as s:
                    s.add(AuditLog(
                        user=user, olt_id=olt.id, olt_name=olt.name, action="push_speed_bulk",
                        target=profile_data["name"], ok=False, commands=list_cmd, output=output[:8000],
                    ))
                    await s.commit()
                continue

            plan = V.plan_push_speed(olt, None, {}, {"profile": profile_data})
            commands = V.validate_commands(plan.commands)

            async def check_missing(cli):
                current = await cli.run(list_cmd, overall=30, idle=10)
                current_names = P.parse_profile_names(current.text) if not current.error and current.complete else []
                if not current_names:
                    raise V.PlanError(
                        f"Could not read the {profile_data['direction']} profile list; no changes were sent"
                    )
                if profile_data["name"] in current_names:
                    raise _ProfileAlreadyPresent

            result = await V.run_plan(conn, commands, plan.rollback, pre=check_missing)
            output = "\n".join(
                f"{'[rollback] ' if entry['rollback'] else ''}{entry['cmd']} -> {entry['output']}"
                for entry in result.log
            ) or result.message
            async with SessionLocal() as s:
                s.add(AuditLog(
                    user=user, olt_id=olt.id, olt_name=olt.name, action="push_speed_bulk",
                    target=profile_data["name"], ok=result.ok,
                    commands="\n".join(V.mask(command) for command in commands),
                    output=V.mask(output)[:8000],
                ))
                await s.commit()

            if not result.ok:
                log.warning("Failed to distribute speed profile %s to OLT %s: %s",
                            profile_data["name"], olt.name, result.message)
                continue

            try:
                saved, message = await zte.save_config(conn)
            except (zte.TelnetError, zte.BusyError, OSError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                saved, message = False, f"{type(exc).__name__}: {exc}"
            async with SessionLocal() as s:
                current_olt = await s.get(OLT, olt.id)
                if current_olt:
                    current_olt.last_save_attempt_at = utcnow()
                    current_olt.last_save_ok = saved
                    current_olt.last_save_msg = message[:300]
                    if saved:
                        current_olt.last_save_at = utcnow()
                s.add(AuditLog(
                    user=user, olt_id=olt.id, olt_name=olt.name, action="save_config",
                    target=f"speed profile {profile_data['name']}", ok=saved,
                    commands=conn.profile.save_cmd, output=message[:8000],
                ))
                await s.commit()
            if not saved:
                log.warning("Profile %s was created on OLT %s but saving the configuration failed: %s",
                            profile_data["name"], olt.name, message)
        except _ProfileAlreadyPresent:
            async with SessionLocal() as s:
                s.add(AuditLog(
                    user=user, olt_id=olt.id, olt_name=olt.name, action="push_speed_bulk",
                    target=profile_data["name"], ok=False,
                    commands="\n".join(V.mask(command) for command in commands),
                    output="A profile with the same name appeared before execution; skipped without changes.",
                ))
                await s.commit()
        except (V.PlanError, zte.TelnetError, zte.BusyError, OSError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            log.warning("Failed to distribute speed profile %s to OLT %s: %s",
                        profile_data["name"], olt.name, exc)
            async with SessionLocal() as s:
                s.add(AuditLog(
                    user=user, olt_id=olt.id, olt_name=olt.name, action="push_speed_bulk",
                    target=profile_data["name"], ok=False,
                    commands="\n".join(V.mask(command) for command in commands),
                    output=V.mask(str(exc))[:8000],
                ))
                await s.commit()


async def replicate_speed_profiles(source_olt_id: int, profiles: dict) -> None:
    """Create profiles found on the source OLT on other OLTs that allow changes."""
    candidates = [(direction, name, kbps)
                  for direction, items in profiles.items() if direction in ("download", "upload")
                  for name, kbps in items.items()]
    if not candidates:
        return

    async with SessionLocal() as s:
        targets = list((await s.scalars(
            select(OLT).where(OLT.id != source_olt_id, OLT.write_enabled.is_(True))
        )).all())

    for olt in targets:
        try:
            conn = zte.conn_from_row(olt)
        except (ValueError, TypeError) as exc:
            log.warning("Auto-push of speed profile to OLT %s skipped: %s", olt.name, exc)
            continue

        for direction in ("upload", "download"):
            direction_candidates = [(name, kbps) for candidate_direction, name, kbps in candidates
                                    if candidate_direction == direction]
            if not direction_candidates:
                continue
            prof = zte.profile_for(olt.cli_family)
            list_cmd = prof.tcont_list_cmd if direction == "upload" else prof.traffic_list_cmd
            try:
                async with zte.links.session(conn, lane="sync") as cli:
                    listed = await cli.run(list_cmd, overall=30, idle=10)
                names = P.parse_profile_names(listed.text) if not listed.error and listed.complete else []
                if not names:
                    log.warning("Auto-push of speed profile %s to OLT %s skipped: could not read %s profile list",
                                direction, olt.name, direction)
                    continue
            except (zte.TelnetError, zte.BusyError, OSError, ValueError, TypeError) as exc:
                log.warning("Failed to read %s profiles on OLT %s; replication skipped: %s",
                            direction, olt.name, exc)
                continue

            for name, kbps in direction_candidates:
                if name in names:
                    continue
                profile = {"name": name, "kbps": kbps, "direction": direction}
                try:
                    plan = V.plan_push_speed(olt, None, {}, {"profile": profile})
                    commands = V.validate_commands(plan.commands)

                    async def check_missing(cli):
                        current = await cli.run(list_cmd, overall=30, idle=10)
                        current_names = P.parse_profile_names(current.text) if not current.error and current.complete else []
                        if not current_names:
                            raise V.PlanError(f"Could not read the {direction} profile list; no changes were sent")
                        if name in current_names:
                            raise _ProfileAlreadyPresent

                    result = await V.run_plan(conn, commands, plan.rollback, pre=check_missing)
                except _ProfileAlreadyPresent:
                    continue
                except (V.PlanError, zte.TelnetError, zte.BusyError, OSError, ValueError, TypeError) as exc:
                    log.warning("Auto-push of speed profile %s to OLT %s failed: %s", name, olt.name, exc)
                    continue

                output = "\n".join(
                    f"{'[rollback] ' if entry['rollback'] else ''}{entry['cmd']} -> {entry['output']}"
                    for entry in result.log
                )
                async with SessionLocal() as s:
                    s.add(AuditLog(
                        user="system", olt_id=olt.id, olt_name=olt.name, action="push_speed_auto",
                        target=name, ok=result.ok, commands="\n".join(V.mask(c) for c in commands),
                        output=V.mask(output or result.message)[:8000],
                    ))
                    await s.commit()
                if not result.ok:
                    log.warning("Auto-push of speed profile %s to OLT %s failed: %s", name, olt.name, result.message)


def apply_item(o: ONU, item: dict, prof_id: dict, partial: bool = False) -> None:
    """Copy parsed OLT configuration into an ONU row. Do not overwrite names changed in the dashboard.
    partial=True (resync one ONU): the ONU interface output does not contain SN/type, so do not clear them."""
    for f in ("sn", "onu_type", "description", "vlan", "conn_type", "pppoe_user", "mgmt_ip", "mgmt_vlan"):
        if partial and f in ("sn", "onu_type"):
            continue
        if getattr(o, f) != item[f]:
            setattr(o, f, item[f])
    nm = item["name"] or item["description"] or item["sn"]
    if not o.name or o.name == o.cfg_name:
        o.name = nm
    o.cfg_name = nm
    if not o.mode and item["mode"]:
        o.mode = item["mode"]
    for col, pname, direction in (("ul_profile_id", item["up_profile"], "upload"), ("dl_profile_id", item["dl_profile"], "download")):
        if pname and (pname, direction) in prof_id:
            setattr(o, col, prof_id[(pname, direction)])


def cards_from_config(cards: list[dict]) -> dict:
    rows = [{"shelf": c["shelf"], "slot": c["slot"], "type": c["type"], "real_type": "", "ports": None, "hw": "",
             "sw": "", "status": "(from config)", "online": None, "role": ""} for c in cards]
    return {"rows": rows, "raw": "Read from 'add-card' in running-config because 'show card' could not be parsed.", "source": "config"}


async def _read_onu_macs(conn, onus: list[dict]) -> tuple[dict, set[tuple], int]:
    """Read MAC addresses per ONU; failed reads do not clear the previous cache."""
    if not conn.profile.mac_cmd:
        return {}, set(), 0

    rows_by_key, scanned, failed = {}, set(), 0
    try:
        async with zte.links.session(conn, wait=60, lane="sync") as cli:
            for item in onus:
                key = (item["shelf"], item["slot"], item["pon"], item["onu_number"])
                cmd = conn.profile.mac_cmd.format(**zte.onu_command_format(conn, key))
                try:
                    result = await cli.run(cmd, overall=30, idle=15)
                except (zte.TelnetError, zte.BusyError, asyncio.TimeoutError, OSError) as exc:
                    log.warning("MAC read stopped for ONU %s on OLT %s: %s", key, conn.name, exc)
                    failed += 1
                    break
                if result.error:
                    log.warning("MAC read failed for ONU %s on OLT %s; continuing to the next ONU", key, conn.name)
                    failed += 1
                    continue
                if not result.complete:
                    log.warning("MAC output for ONU %s on OLT %s is incomplete; preserving the previous cache", key, conn.name)
                    failed += 1
                    break
                rows_by_key[key] = P.parse_mac_table(result.text)
                scanned.add(key)
    except (zte.TelnetError, zte.BusyError, asyncio.TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
        log.warning("MAC scan failed on OLT %s; preserving the previous cache for unscanned ONUs: %s",
                    conn.name, exc)
        failed += 1
    return rows_by_key, scanned, failed


async def store_onu_macs(session, onu_id: int, rows: list[dict]) -> bool:
    """Store MAC results from an ONU status read; return True if the cache changed."""
    unique = {row["mac"]: row["mac"] for row in rows}
    existing = list(await session.scalars(select(OnuMacAddress).where(OnuMacAddress.onu_id == onu_id)))
    if {row.mac for row in existing} == set(unique):
        return False
    await session.execute(delete(OnuMacAddress).where(OnuMacAddress.onu_id == onu_id))
    now = utcnow()
    session.add_all(OnuMacAddress(onu_id=onu_id, mac=mac,
                                  mac_compact=re.sub(r"[^0-9A-Fa-f]", "", mac).upper(),
                                  updated_at=now)
                    for mac in unique)
    return True


# ───────────────────────── full sync ─────────────────────────
async def run_full_sync(olt_id: int) -> None:
    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if not olt:
            return
        olt.status, olt.status_msg, olt.sync_requested = "syncing", "Reading running-config and ONU status…", False
        await s.commit()
        await bus.publish(olt_event(olt))
        try:
            conn = zte.conn_from_row(olt)
        except ValueError as exc:
            await _mark(olt_id, "offline", str(exc))
            return

    info: dict = {}
    try:
        info = await zte.snmp_info(conn)
    except SnmpError as exc:
        log.info("SNMP info %s: %s", conn.name, exc)

    try:
        data = await zte.read_config_and_states(conn)
    except Exception as exc:
        await _mark(olt_id, "offline", f"Sync failed: {exc or exc.__class__.__name__}")
        return

    await import_masters(data.extras)
    await replicate_speed_profiles(olt_id, data.extras.get("speed", {}))
    mac_targets = [item for item in data.onus
                   if not data.states or phase_to_status(data.states.get(
                       (item["shelf"], item["slot"], item["pon"], item["onu_number"]), "")) not in
                   ("offline", "los", "power_fail")]
    mac_rows, mac_scanned, mac_failures = await _read_onu_macs(conn, mac_targets)
    mac_count = 0
    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if not olt:
            return
        prof_id = {(p.name, p.direction): p.id for p in await s.scalars(select(SpeedProfile))}
        existing = {(o.shelf, o.slot, o.pon, o.onu_number): o
                    for o in await s.scalars(select(ONU).where(ONU.olt_id == olt_id))}
        now, seen, synced_onus = utcnow(), set(), {}
        for item in data.onus:
            key = (item["shelf"], item["slot"], item["pon"], item["onu_number"])
            seen.add(key)
            phase = data.states.get(key, "")
            status = phase_to_status(phase) if phase else "unknown"
            o = existing.get(key)
            if o is None:
                o = ONU(olt_id=olt_id, shelf=key[0], slot=key[1], pon=key[2], onu_number=key[3])
                s.add(o)
            apply_item(o, item, prof_id)
            if o.status != status or o.phase_state != phase:
                o.status_changed_at = now
            o.status, o.phase_state, o.updated_at = status, phase, now
            synced_onus[key] = o
        if mac_scanned:
            await s.flush()
            mac_onu_ids = [synced_onus[key].id for key in mac_scanned if key in synced_onus]
            if mac_onu_ids:
                await s.execute(delete(OnuMacAddress).where(OnuMacAddress.onu_id.in_(mac_onu_ids)))
                unique_macs = {}
                for key in mac_scanned:
                    if key not in synced_onus:
                        continue
                    onu_id = synced_onus[key].id
                    for row in mac_rows.get(key, []):
                        unique_macs[(onu_id, row["mac"])] = row["mac"]
                mac_count = len(unique_macs)
                s.add_all(OnuMacAddress(onu_id=onu_id, mac=mac, mac_compact=re.sub(r"[^0-9A-Fa-f]", "", mac).upper(),
                                        updated_at=now)
                          for (onu_id, mac) in unique_macs)
        removed = 0
        if data.complete and data.onus:  # Do not delete anything if output is truncated or empty.
            for key, o in existing.items():
                if key not in seen:
                    await s.delete(o)
                    removed += 1
        olt.status = "online"
        olt.last_full_sync_at = now
        if info:
            olt.sys_descr, olt.uptime_s = info["sys_descr"], info["uptime_s"]
            olt.sw_version = parse_sw_version(info["sys_descr"]) or olt.sw_version
        await save_info(s, olt_id, "pon_cfg", data.extras.get("pon", {}))
        await save_info(s, olt_id, "uplinks", data.extras.get("uplinks", {}))
        await save_info(s, olt_id, "vlans", data.extras.get("vlans", {}))
        await save_info(s, olt_id, "onu_types", data.extras.get("onu_types", {}))
        if data.cards:
            await save_info(s, olt_id, "cards", data.cards)
            main_sw = next((c["sw"] for c in data.cards["rows"] if c["role"] == "Main" and c["sw"]), "")
            if main_sw and not olt.sw_version:
                olt.sw_version = main_sw.lstrip("Vv")
        elif data.extras.get("cards"):
            await save_info(s, olt_id, "cards", cards_from_config(data.extras["cards"]))
        no_state = "" if data.states else " (ONU status not yet available from CLI; awaiting poll)"
        olt.status_msg = (f"Sync OK: {len(data.onus)} ONUs"
                          f"{f', {removed} removed' if removed else ''}{no_state}")
        if not data.complete:
            olt.status_msg += " — warning: running-config output is truncated"
        await s.commit()
        await bus.publish(olt_event(olt))
        await _publish_counts(s, olt_id)
        await bus.publish({"type": "onus_reload", "olt_id": olt_id})
    mac_note = (f"MAC: {mac_count} addresses; checked {len(mac_scanned)}/{len(mac_targets)} active ONUs"
                f"; skipped {len(data.onus) - len(mac_targets)} inactive; {mac_failures} failed"
                if conn.profile.mac_cmd else "MAC: command not enabled")
    await poll_once(olt_id, 0, status_note=mac_note)  # Update status and signal now instead of waiting for the next cycle.


# ───────────────────────── poll cepat ─────────────────────────
def _moved(old: float | None, new: float | None) -> bool:
    return new is not None and (old is None or abs(old - new) >= 0.1)


def effective_status(status: str, rx_now: tuple | None) -> str:
    """Signal read NOW (this poll, not stale database data) means the ONU is alive and connected.
    Do not override LOS/power failure; only correct 'offline'/'unknown' from an incorrect/unrecognized phase state."""
    if status in ("offline", "unknown") and rx_now and any(v is not None for v in rx_now):
        return "online"
    return status


async def poll_once(olt_id: int, tick: int, status_note: str = "") -> None:
    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if not olt:
            return
        try:
            conn = zte.conn_from_row(olt)
        except ValueError as exc:
            await _mark(olt_id, "offline", str(exc))
            return

    states, rx, source, snmp_err = {}, {}, "", ""
    try:
        states, rx = await zte.poll_snmp(conn)
        source = "snmp"
    except Exception as exc:  # SNMP failure or OID mismatch -> fall back to CLI.
        snmp_err = str(exc) or exc.__class__.__name__
        try:
            async with SessionLocal() as s:
                ports = [tuple(p) for p in (await s.execute(select(ONU.shelf, ONU.slot, ONU.pon)
                                                            .where(ONU.olt_id == olt_id).distinct())).all()]
            states = await zte.poll_cli_states(conn, ports)
            source = "cli"
            if tick % max(1, settings.cli_rx_every) == 0:
                rx = await zte.poll_cli_rx(conn, ports)
        except Exception as exc2:
            await _mark(olt_id, "offline", f"Poll failed — SNMP: {snmp_err}; CLI: {exc2 or exc2.__class__.__name__}")
            return

    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if not olt:
            return
        rows = (await s.execute(select(ONU.id, ONU.shelf, ONU.slot, ONU.pon, ONU.onu_number, ONU.status,
                                       ONU.phase_state, ONU.rx_power, ONU.olt_rx_power, ONU.status_changed_at)
                                .where(ONU.olt_id == olt_id))).all()
        by_key = {(r.shelf, r.slot, r.pon, r.onu_number): r for r in rows}
        now, updates, changes, unknown = utcnow(), [], [], 0
        for key, (status, phase) in states.items():
            r = by_key.get(key)
            if r is None:
                unknown += 1
                continue
            status = effective_status(status, rx.get(key))
            upd = {"id": r.id, "status": r.status, "phase_state": r.phase_state, "rx_power": r.rx_power,
                   "olt_rx_power": r.olt_rx_power, "status_changed_at": r.status_changed_at, "updated_at": now}
            dirty = False
            if r.status != status or r.phase_state != phase:
                upd.update(status=status, phase_state=phase, status_changed_at=now)
                dirty = True
            if key in rx:
                onu_rx, olt_rx = rx[key]
                if _moved(r.rx_power, onu_rx):
                    upd["rx_power"], dirty = onu_rx, True
                if _moved(r.olt_rx_power, olt_rx):
                    upd["olt_rx_power"], dirty = olt_rx, True
            if dirty:
                updates.append(upd)
                changes.append({k: upd[k] for k in ("id", "status", "phase_state", "rx_power", "olt_rx_power")})
        if updates:
            await s.execute(update(ONU), updates)  # Bulk UPDATE by primary key, changed rows only.
        olt.status, olt.poll_source, olt.last_poll_at = "online", source, now
        olt.status_msg = f"Poll {source.upper()} OK: {len(states)} ONUs, {len(changes)} changed" + (
            f" (SNMP failed: {snmp_err})" if snmp_err else "") + (
            f" — {status_note}" if status_note else "")
        if unknown and (olt.last_full_sync_at is None or now - olt.last_full_sync_at > timedelta(minutes=10)):
            olt.sync_requested = True  # A new ONU exists on the OLT but is not in the database yet.
        await s.commit()
        await bus.publish(olt_event(olt))
        for i in range(0, len(changes), 200):
            await bus.publish({"type": "onus", "olt_id": olt_id, "changes": changes[i:i + 200]})
        if changes:
            await _publish_counts(s, olt_id)


# ───────────────────────── save config ─────────────────────────
def _tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _to_local(dt_utc_naive: datetime) -> datetime:
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(_tz())


def autosave_target(now_local: datetime) -> datetime:
    h, m = (int(x) for x in settings.autosave_time.split(":"))
    return now_local.replace(hour=h, minute=m, second=0, microsecond=0)


def save_due(last_save_at, last_attempt_at, now_local: datetime, retry_minutes: int = 30) -> bool:
    """If today's scheduled time has passed without a successful save, retry every 30 minutes."""
    target = autosave_target(now_local)
    if now_local < target:
        return False
    if last_save_at and _to_local(last_save_at) >= target:
        return False
    if last_attempt_at and (now_local - _to_local(last_attempt_at)) < timedelta(minutes=retry_minutes):
        return False
    return True


async def do_save(olt_id: int) -> tuple[bool, str]:
    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if not olt:
            return False, "OLT not found"
        olt.last_save_attempt_at = utcnow()
        await s.commit()
        try:
            conn = zte.conn_from_row(olt)
        except ValueError as exc:
            return False, str(exc)
    try:
        ok, msg = await zte.save_config(conn)
    except Exception as exc:
        ok, msg = False, str(exc) or exc.__class__.__name__
    async with SessionLocal() as s:
        olt = await s.get(OLT, olt_id)
        if olt:
            olt.last_save_ok, olt.last_save_msg = ok, msg[:300]
            if ok:
                olt.last_save_at = utcnow()
            await s.commit()
            await bus.publish(olt_event(olt))
    log.info("Save config %s: %s %s", olt_id, "OK" if ok else "GAGAL", msg[:80])
    return ok, msg
