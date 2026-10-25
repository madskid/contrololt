"""Manually confirmed ONU moves."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.drivers import zte
from app.drivers.telnet import BusyError, TelnetError
from app.models import OLT, ONU, OnuMoveJob, utcnow
from app.security import require_admin
from app.services import onu_moves, provision as V, sync

router = APIRouter(prefix="/api/onu-moves", dependencies=[Depends(require_admin)])


class MoveScheduleIn(BaseModel):
    source_onu_id: int = Field(gt=0)
    target_olt_id: int = Field(gt=0)
    target_slot: int = Field(ge=1, le=16)
    target_pon: int = Field(ge=1, le=32)


@router.get("")
async def list_moves(s: AsyncSession = Depends(get_session)):
    jobs = (await s.scalars(select(OnuMoveJob).order_by(OnuMoveJob.created_at.desc()).limit(100))).all()
    olt_ids = {job.source_olt_id for job in jobs} | {job.target_olt_id for job in jobs}
    names = {o.id: o.name for o in (await s.scalars(select(OLT).where(OLT.id.in_(olt_ids)))).all()} if olt_ids else {}
    return {"rows": [{
        "id": job.id, "sn": job.sn, "source_olt": names.get(job.source_olt_id, f"OLT {job.source_olt_id}"),
        "source_slot": job.source_slot, "source_pon": job.source_pon,
        "source_onu_number": job.source_onu_number,
        "target_olt": names.get(job.target_olt_id, f"OLT {job.target_olt_id}"),
        "target_slot": job.target_slot, "target_pon": job.target_pon,
        "status": job.status, "scheduled_at": sync.iso(job.scheduled_at),
        "created_at": sync.iso(job.created_at), "finished_at": sync.iso(job.finished_at),
        "created_by": job.created_by, "result": job.result,
    } for job in jobs]}


@router.post("", status_code=201)
async def schedule_move(b: MoveScheduleIn, user: str = Depends(require_admin), s: AsyncSession = Depends(get_session)):
    onu = await s.get(ONU, b.source_onu_id)
    target = await s.get(OLT, b.target_olt_id)
    if not onu:
        raise HTTPException(404, "Source ONU not found")
    source = await s.get(OLT, onu.olt_id)
    if not target:
        raise HTTPException(404, "Destination OLT not found")
    if not source or not source.write_enabled or not target.write_enabled:
        raise HTTPException(409, "Allow changes to OLT must be enabled on both source and destination OLTs.")
    if (source.id == target.id and (onu.slot, onu.pon) == (b.target_slot, b.target_pon)):
        raise HTTPException(422, "Source and destination PON interfaces are the same.")
    in_progress = (await s.scalars(select(OnuMoveJob.id).where(
        OnuMoveJob.source_onu_id == onu.id, OnuMoveJob.status.in_(("scheduled", "running"))
    ).limit(1))).first()
    if in_progress:
        raise HTTPException(409, "This ONU already has a scheduled or running move.")
    duplicate = (await s.scalars(select(ONU.id).where(
        ONU.olt_id == target.id, func.upper(func.trim(ONU.sn)) == onu.sn.strip().upper(), ONU.id != onu.id
    ).limit(1))).first()
    if duplicate:
        raise HTTPException(409, "This serial number is already registered on the destination OLT.")

    try:
        snapshot = await onu_moves.read_move_config(onu, source)
    except V.PlanError as exc:
        raise HTTPException(422, str(exc))
    except (TelnetError, BusyError, OSError, ValueError, asyncio.TimeoutError) as exc:
        raise HTTPException(502, f"Could not read the ONU configuration from the source OLT: {exc}")

    job = OnuMoveJob(
        source_onu_id=onu.id, source_olt_id=source.id, source_shelf=onu.shelf,
        source_slot=onu.slot, source_pon=onu.pon, source_onu_number=onu.onu_number,
        target_olt_id=target.id, target_slot=b.target_slot, target_pon=b.target_pon,
        sn=onu.sn, config_snapshot=json.dumps(snapshot),
        dashboard_snapshot=json.dumps({
            "name": onu.name, "zone_id": onu.zone_id, "odb_id": onu.odb_id,
            "address": onu.address, "contact": onu.contact,
            "external_id": onu.external_id, "auth_date": sync.iso(onu.auth_date),
        }),
        created_by=user, scheduled_at=utcnow(),
    )
    s.add(job)
    await s.commit()
    await s.refresh(job)
    return {"id": job.id, "sn": job.sn, "status": job.status, "scheduled_at": sync.iso(job.scheduled_at),
            "source": f"{source.name} {onu.slot}/{onu.pon}:{onu.onu_number}",
            "target": f"{target.name} {b.target_slot}/{b.target_pon}",
            "message": "Move manual dimulai sekarang."}


@router.delete("/{job_id}")
async def cancel_move(job_id: int, s: AsyncSession = Depends(get_session)):
    result = await s.execute(
        update(OnuMoveJob)
        .where(OnuMoveJob.id == job_id, OnuMoveJob.status == "scheduled")
        .values(status="cancelled", finished_at=utcnow(), result="Cancelled by administrator.")
    )
    await s.commit()
    if result.rowcount:
        return {"ok": True}
    job = await s.get(OnuMoveJob, job_id)
    if not job:
        raise HTTPException(404, "Move schedule not found")
    raise HTTPException(409, f"Only scheduled moves can be cancelled (current status: {job.status}).")
