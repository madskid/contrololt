"""Persistent manual ONU moves between OLT PON interfaces."""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.bus import bus
from app.db import SessionLocal
from app.drivers import zte, zte_parsers as P
from app.models import AuditLog, OLT, ONU, OnuMoveJob, OnuType, SpeedProfile, utcnow
from app.services import provision as V

log = logging.getLogger("smartolt.onu_moves")
_SERVICE_RE = re.compile(r"^service\s+(\S+)\s+gemport\s+\d+", re.I)


async def read_move_config(onu: ONU, olt: OLT) -> dict:
    """Read and validate the current bridge config before accepting a move schedule."""
    key = (onu.shelf, onu.slot, onu.pon, onu.onu_number)
    conn = zte.conn_from_row(olt)
    raw = await zte.read_onu_cfg(conn, key)
    item = next((row for row in P.parse_running_config(raw)
                 if (row["shelf"], row["slot"], row["pon"], row["onu_number"]) == key), None)
    async with zte.links.session(conn, wait=30, lane="check") as cli:
        lookup = await cli.run(f"show gpon onu by sn {onu.sn.strip().upper()}", overall=20, idle=10)
    found = re.search(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", lookup.text, re.I)
    if (not item or lookup.error or not lookup.complete or not found or
            tuple(int(value) for value in found.groups()) != key or
            (item["sn"] and item["sn"].strip().upper() != onu.sn.strip().upper())):
        raise V.PlanError("Could not verify the ONU serial number and current configuration on the source OLT.")
    if item["mode"] == "routing" or item["conn_type"]:
        raise V.PlanError("Automatic move currently supports bridge ONUs only. Routing PPPoE credentials cannot be safely copied.")

    in_mng = False
    service = ""
    veip = False
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if line == "!":
            in_mng = False
            continue
        if line.lower().startswith("pon-onu-mng "):
            in_mng = True
            continue
        if in_mng:
            match = _SERVICE_RE.match(line)
            if match and not service:
                service = match.group(1)
            if re.fullmatch(r"vlan port veip_\d+ mode hybrid", line, re.I):
                veip = True

    async with SessionLocal() as s:
        profiles = {p.id: p for p in (await s.scalars(select(SpeedProfile))).all()}
    onu_type = item["onu_type"] or onu.onu_type
    ul = item["up_profile"] or (profiles.get(onu.ul_profile_id).name if onu.ul_profile_id in profiles else "")
    dl = item["dl_profile"] or (profiles.get(onu.dl_profile_id).name if onu.dl_profile_id in profiles else "")
    vlan = item["vlan"] or onu.vlan
    if not onu_type or not vlan or not ul or not dl:
        raise V.PlanError("Source configuration is missing ONU type, VLAN, or speed profile; cannot safely schedule this move.")
    return {
        "sn": onu.sn.strip().upper(),
        "onu_type": onu_type,
        "name": item["name"] or item["description"] or onu.name or onu.sn,
        "vlan": int(vlan),
        "mode": "onu_pppoe" if veip else "bridge",
        "service": service or ("PPPOE" if veip else "INT"),
        "ul_name": ul,
        "dl_name": dl,
    }


async def _audit(olt: OLT, action: str, target: str, ok: bool, commands: list[str], result: str) -> None:
    async with SessionLocal() as s:
        s.add(AuditLog(user="system", olt_id=olt.id, olt_name=olt.name, action=action, target=target,
                       ok=ok, commands="\n".join(V.mask(c) for c in commands),
                       output=V.mask(result)[:8000]))
        await s.commit()


async def _registered_locations(olt: OLT, sn: str) -> list[tuple[int, int, int, int]]:
    async with zte.links.session(zte.conn_from_row(olt), wait=30, lane="check") as cli:
        result = await cli.run(f"show gpon onu by sn {sn}", overall=20, idle=10)
    if result.error or not result.complete:
        raise V.PlanError(f"Could not verify ONU {sn} registration state on OLT {olt.name}.")
    return [tuple(map(int, match.groups())) for match in
            re.finditer(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", result.text, re.I)]


async def _execute_move(job_id: int) -> None:
    async with SessionLocal() as s:
        job = await s.get(OnuMoveJob, job_id)
        if not job or job.status != "running":
            return
        source = await s.get(OLT, job.source_olt_id)
        target = await s.get(OLT, job.target_olt_id)
        onu = await s.get(ONU, job.source_onu_id)
        snapshot = json.loads(job.config_snapshot or "{}")
        dashboard = json.loads(job.dashboard_snapshot or "{}")
        actor_target = f"{job.sn} {job.source_slot}/{job.source_pon} -> {job.target_slot}/{job.target_pon}"
        if not source or not target or not onu:
            raise V.PlanError("Source ONU or source/destination OLT no longer exists.")
        if not source.write_enabled or not target.write_enabled:
            raise V.PlanError("Source and destination OLTs must both have OLT writes enabled.")
        if onu.olt_id != source.id or (onu.slot, onu.pon, onu.onu_number, onu.sn.upper()) != (
                job.source_slot, job.source_pon, job.source_onu_number, job.sn.upper()):
            raise V.PlanError("ONU source position or serial changed after this move was scheduled.")
        dashboard.update({
            "name": onu.name, "zone_id": onu.zone_id, "odb_id": onu.odb_id,
            "address": onu.address, "contact": onu.contact, "external_id": onu.external_id,
            "auth_date": onu.auth_date.isoformat() + "Z" if onu.auth_date else dashboard.get("auth_date"),
        })
        if (job.target_slot, job.target_pon) == (job.source_slot, job.source_pon) and source.id == target.id:
            raise V.PlanError("Source and destination PON interfaces are the same.")
        source_conn, target_conn = zte.conn_from_row(source), zte.conn_from_row(target)
        current = await read_move_config(onu, source)
        if current != snapshot:
            raise V.PlanError("ONU service configuration changed after scheduling. Cancel and create a new move.")

        async with SessionLocal() as s:
            used = set(await s.scalars(select(ONU.onu_number).where(
                ONU.olt_id == target.id, ONU.slot == job.target_slot, ONU.pon == job.target_pon)))
            if source.id == target.id:
                used.discard(job.source_onu_number)
            profile_ids = {(p.name, p.direction): p.id for p in (await s.scalars(select(SpeedProfile))).all()}
            onu_type = (await s.scalars(select(OnuType).where(
                OnuType.name == snapshot["onu_type"]
            ))).first()
            source_port_used = set(await s.scalars(select(ONU.onu_number).where(
                ONU.olt_id == source.id, ONU.slot == job.source_slot, ONU.pon == job.source_pon,
                ONU.id != onu.id)))
        ul_id = profile_ids.get((snapshot["ul_name"], "upload"))
        dl_id = profile_ids.get((snapshot["dl_name"], "download"))
        params = {
            **snapshot, "slot": job.target_slot, "pon": job.target_pon,
            "ul_profile_id": ul_id, "dl_profile_id": dl_id,
            "zone_id": dashboard.get("zone_id"), "odb_id": dashboard.get("odb_id"),
            "address": dashboard.get("address", ""), "contact": dashboard.get("contact", ""),
            "external_id": dashboard.get("external_id", ""),
        }
        ctx = {"used": used, "ul_name": snapshot["ul_name"], "dl_name": snapshot["dl_name"],
               "eth_ports": onu_type.eth_ports if onu_type else 1, "type_unknown": onu_type is None}
        target_plan = V.plan_register(target, params, ctx)

    same_olt = source.id == target.id
    if same_olt:
        async with zte.links.session(target_conn, wait=90) as cli:
            await V.verify_speed_profiles(cli, target, snapshot["ul_name"], snapshot["dl_name"])
            state = await cli.run(
                f"show gpon onu state {V._olt_if(target, job.target_slot, job.target_pon)}",
                overall=60, idle=20)
            if state.error or not state.complete:
                raise V.PlanError("Could not verify the destination PON before removing the source ONU.")
            ctx["used"].update(key[3] for key in P.parse_onu_state(state.text))
            target_plan = V.plan_register(target, params, ctx)

    if same_olt:
        delete_plan = V.plan_delete(source, onu, {}, {})
        removed = await V.run_plan(source_conn, delete_plan.commands, delete_plan.rollback)
        await _audit(source, "onu_move_remove_source", actor_target, removed.ok,
                     delete_plan.commands, removed.message)
        if not removed.ok:
            raise V.PlanError(f"Could not remove ONU from the source PON: {removed.message}")

    async def check_target(cli):
        await V.verify_speed_profiles(cli, target, snapshot["ul_name"], snapshot["dl_name"])
        result = await cli.run(f"show gpon onu by sn {job.sn}", overall=20, idle=10)
        if result.error or not result.complete:
            raise V.PlanError("Could not verify the destination OLT for duplicate ONU serials.")
        if re.search(r"gpon[-_]onu[-_]\d+/\d+/\d+:\d+", result.text):
            raise V.PlanError(f"ONU {job.sn} is already registered on the destination OLT.")
        state = await cli.run(f"show gpon onu state {V._olt_if(target, job.target_slot, job.target_pon)}",
                              overall=60, idle=20)
        if state.error or not state.complete:
            raise V.PlanError("Could not verify free ONU indexes on the destination PON.")
        live = {key[3] for key in P.parse_onu_state(state.text)}
        if target_plan.meta["number"] in live:
            ctx["used"].update(live)
            replacement = V.plan_register(target, params, ctx)
            target_plan.meta.update(replacement.meta)
            return replacement.commands, replacement.rollback
        return None

    try:
        result = await V.run_plan(target_conn, target_plan.commands, target_plan.rollback, pre=check_target)
        if result.ok:
            locations = await _registered_locations(target, job.sn)
            expected = (target.shelf, job.target_slot, job.target_pon, target_plan.meta["number"])
            if locations != [expected]:
                raise V.PlanError(
                    f"Destination verification failed for {job.sn}; expected only {expected}, found {locations}."
                )
    except Exception as exc:
        if not same_olt:
            raise
        restore_params = {
            **snapshot, "slot": job.source_slot, "pon": job.source_pon,
            "ul_profile_id": ul_id, "dl_profile_id": dl_id,
            "zone_id": dashboard.get("zone_id"), "odb_id": dashboard.get("odb_id"),
            "address": dashboard.get("address", ""), "contact": dashboard.get("contact", ""),
            "external_id": dashboard.get("external_id", ""),
        }
        restore_ctx = {
            "used": source_port_used | set(range(1, job.source_onu_number)),
            "ul_name": snapshot["ul_name"], "dl_name": snapshot["dl_name"],
            "eth_ports": onu_type.eth_ports if onu_type else 1, "type_unknown": onu_type is None,
        }
        restore = V.plan_register(source, restore_params, restore_ctx)
        restored = await V.run_plan(source_conn, restore.commands, restore.rollback)
        await _audit(source, "onu_move_restore_source", actor_target, restored.ok, restore.commands, restored.message)
        if not restored.ok:
            raise V.PlanError(f"Destination move failed ({exc}); source restoration also failed ({restored.message}).") from exc
        raise V.PlanError(f"Destination move failed ({exc}); source ONU was restored.") from exc
    output = "\n".join(f"{entry['cmd']} -> {entry['output']}" for entry in result.log)
    await _audit(target, "onu_move_register_target", actor_target, result.ok, result.replaced[0], output or result.message)
    if not result.ok:
        if same_olt:
            restore_params = {
                **snapshot, "slot": job.source_slot, "pon": job.source_pon,
                "ul_profile_id": ul_id, "dl_profile_id": dl_id,
                "zone_id": dashboard.get("zone_id"), "odb_id": dashboard.get("odb_id"),
                "address": dashboard.get("address", ""), "contact": dashboard.get("contact", ""),
                "external_id": dashboard.get("external_id", ""),
            }
            restore_ctx = {
                "used": source_port_used | set(range(1, job.source_onu_number)),
                "ul_name": snapshot["ul_name"], "dl_name": snapshot["dl_name"],
                "eth_ports": onu_type.eth_ports if onu_type else 1, "type_unknown": onu_type is None,
            }
            restore = V.plan_register(source, restore_params, restore_ctx)
            restored = await V.run_plan(source_conn, restore.commands, restore.rollback)
            await _audit(source, "onu_move_restore_source", actor_target, restored.ok,
                         restore.commands, restored.message)
            if not restored.ok:
                raise V.PlanError(f"Destination registration failed ({result.message}); source restoration also failed ({restored.message}).")
            raise V.PlanError(f"Destination registration failed ({result.message}); source ONU was restored.")
        raise V.PlanError(f"Destination registration failed: {result.message}; source ONU remains registered.")

    if not same_olt:
        delete_plan = V.plan_delete(source, onu, {}, {})
        try:
            removed = await V.run_plan(source_conn, delete_plan.commands, delete_plan.rollback)
        except Exception as exc:
            raise V.PlanError(
                f"Destination ONU was registered, but source removal could not be confirmed: {exc}. "
                "Check both OLTs manually."
            ) from exc
        await _audit(source, "onu_move_remove_source", actor_target, removed.ok,
                     delete_plan.commands, removed.message)
        if not removed.ok:
            raise V.PlanError(f"Destination ONU was registered, but source removal failed: {removed.message}. Check both OLTs manually.")
        remaining = await _registered_locations(source, job.sn)
        if remaining:
            raise V.PlanError(
                f"Destination ONU was registered, but source OLT still reports {job.sn} at {remaining}. "
                "Source cleanup is incomplete; check both OLTs manually."
            )

    async with SessionLocal() as s:
        current_onu = await s.get(ONU, job.source_onu_id)
        target_onu = (await s.scalars(select(ONU).where(
            ONU.olt_id == target.id, ONU.slot == job.target_slot, ONU.pon == job.target_pon,
            ONU.onu_number == target_plan.meta["number"], ONU.sn == snapshot["sn"],
        ).limit(1))).first()
        if target_onu and current_onu and target_onu.id != current_onu.id:
            await s.delete(current_onu)
            current_onu = target_onu
        elif not current_onu:
            current_onu = target_onu
        if not current_onu:
            current_onu = ONU(olt_id=target.id, shelf=target.shelf, slot=job.target_slot,
                              pon=job.target_pon, onu_number=target_plan.meta["number"])
            s.add(current_onu)
        current_onu.olt_id = target.id
        current_onu.shelf = target.shelf
        current_onu.slot = job.target_slot
        current_onu.pon = job.target_pon
        current_onu.onu_number = target_plan.meta["number"]
        current_onu.sn = snapshot["sn"]
        current_onu.onu_type = snapshot["onu_type"]
        current_onu.name = dashboard.get("name") or snapshot["name"]
        current_onu.cfg_name = snapshot["name"]
        current_onu.description = snapshot["name"]
        current_onu.vlan = snapshot["vlan"]
        current_onu.mode = "bridge"
        current_onu.conn_type = ""
        current_onu.pppoe_user = ""
        current_onu.ul_profile_id = ul_id
        current_onu.dl_profile_id = dl_id
        current_onu.status = "unknown"
        current_onu.updated_at = utcnow()
        current_onu.zone_id = dashboard.get("zone_id")
        current_onu.odb_id = dashboard.get("odb_id")
        current_onu.address = dashboard.get("address", "")
        current_onu.contact = dashboard.get("contact", "")
        current_onu.external_id = dashboard.get("external_id", "")
        if dashboard.get("auth_date"):
            current_onu.auth_date = datetime.fromisoformat(dashboard["auth_date"].removesuffix("Z"))
        olt_ids = {source.id, target.id}
        for olt_id in olt_ids:
            olt_row = await s.get(OLT, olt_id)
            if olt_row:
                olt_row.sync_requested = True
        await s.commit()
    await bus.publish({"type": "onus_reload", "olt_id": source.id})
    if target.id != source.id:
        await bus.publish({"type": "onus_reload", "olt_id": target.id})


async def process_due_moves() -> None:
    async with SessionLocal() as s:
        disabled_auto = await s.execute(update(OnuMoveJob).where(
            OnuMoveJob.status == "scheduled", OnuMoveJob.created_by == "auto-move"
        ).values(
            status="cancelled", finished_at=utcnow(),
            result="Automatic moves are disabled; manual confirmation is required.",
        ))
        if disabled_auto.rowcount:
            log.info("Cancelled %d pending automatic ONU move(s)", disabled_auto.rowcount)
            await s.commit()
        due_ids = list(await s.scalars(select(OnuMoveJob.id).where(
            OnuMoveJob.status == "scheduled", OnuMoveJob.scheduled_at <= utcnow()
        ).order_by(OnuMoveJob.scheduled_at).limit(10)))
        interrupted = await s.execute(update(OnuMoveJob).where(
            OnuMoveJob.status == "running", OnuMoveJob.started_at < utcnow() - timedelta(minutes=30)
        ).values(
            status="failed", finished_at=utcnow(),
            result="Worker stopped during execution; inspect both OLTs before attempting another move.",
        ))
        if interrupted.rowcount:
            log.error("Marked %d interrupted ONU move(s) failed for manual OLT verification", interrupted.rowcount)
            await s.commit()
    for job_id in due_ids:
        async with SessionLocal() as s:
            claimed = await s.execute(update(OnuMoveJob).where(
                OnuMoveJob.id == job_id, OnuMoveJob.status == "scheduled"
            ).values(status="running", started_at=utcnow()))
            await s.commit()
            if not claimed.rowcount:
                continue
        try:
            await _execute_move(job_id)
            status, result = "completed", "ONU move completed successfully."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("ONU move job %s failed", job_id)
            status, result = "failed", str(exc)[:2000]
        async with SessionLocal() as s:
            job = await s.get(OnuMoveJob, job_id)
            if job and job.status == "running":
                job.status, job.result, job.finished_at = status, result, utcnow()
                await s.commit()


async def move_scheduler_loop() -> None:
    while True:
        try:
            await process_due_moves()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ONU move scheduler error")
        await asyncio.sleep(1)
