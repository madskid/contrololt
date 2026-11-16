"""MikroTik Router management API.

Provides endpoints for:
- Router CRUD (add / edit / delete / test connection)
- PPPoE secret (user) management: add, edit, delete, copy, change profile
- PPPoE active session listing + disconnect (to force redial)
- PPPoE profile listing (from router) with local cache sync
- MAC address lookup: given a PPPoE username, find the MAC and match it
  against the OLT's ONU MAC address table to surface the ONU/SN.
"""
import asyncio
import ipaddress
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.drivers.mikrotik import MikroClient, MikroError
from app.models import (MikroPppoeLastCaller, MikroProfile, MikroRouter, OLT, ONU,
                        OnuMacAddress, OnuWanIp, utcnow)
from app.security import dec, enc, require_admin, require_user

log = logging.getLogger("smartolt.api_mikrotik")

router = APIRouter(prefix="/api/mikrotik", dependencies=[Depends(require_user)])
_ROUTER_NAME_NOISE = {"access", "cdata", "core", "epon", "gpon", "hioso", "mikrotik",
                      "olt", "onu", "pppoe", "router", "rtr", "zte"}


# ─── helpers ──────────────────────────────────────────────────────────────────

def _router_dict(r: MikroRouter) -> dict:
    return {
        "id": r.id, "name": r.name, "ip": r.ip,
        "api_port": r.api_port, "api_user": r.api_user, "use_ssl": r.use_ssl,
        "status": r.status, "identity": r.identity, "ros_version": r.ros_version,
        "board": r.board, "uptime": r.uptime,
        "last_check_at": r.last_check_at.isoformat() if r.last_check_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _client(r: MikroRouter) -> MikroClient:
    return MikroClient(r.ip, r.api_user, dec(r.api_pass_enc), r.api_port, r.use_ssl)


def _router_location_tokens(name: str) -> set[str]:
    return {token for token in re.findall(r"[a-z]+", name.casefold())
            if token not in _ROUTER_NAME_NOISE}


def _router_matches_olt(router_name: str, olt_name: str) -> bool:
    router_tokens = _router_location_tokens(router_name)
    return bool(router_tokens) and router_tokens <= _router_location_tokens(olt_name)


def _has_ambiguous_pppoe_matches(matches: list[dict]) -> bool:
    usernames = {match["username"].strip().casefold() for match in matches if match["username"].strip()}
    return len(usernames) > 1


def _one_pppoe_match_per_user(matches: list[dict]) -> list[dict]:
    unique = {}
    for match in sorted(
        matches,
        key=lambda row: (
            row["username"].strip().casefold(),
            row["router"].casefold(),
            row["mac_compact"],
            row["ip"],
        ),
    ):
        unique.setdefault(match["username"].strip().casefold(), match)
    return list(unique.values())


def _routers_for_olt(routers: list[MikroRouter], olt: OLT) -> list[MikroRouter]:
    if olt.vendor != "cdata":
        return routers
    if olt.mikrotik_router_id is not None:
        return [router for router in routers if router.id == olt.mikrotik_router_id]
    return [router for router in routers if _router_matches_olt(router.name, olt.name)]


async def _get_router(router_id: int, s: AsyncSession) -> MikroRouter:
    r = await s.get(MikroRouter, router_id)
    if not r:
        raise HTTPException(404, "Router not found")
    return r


async def _commit(s: AsyncSession, what: str = "item"):
    try:
        await s.commit()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(409, f"{what} already exists")


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class RouterIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    ip: str = Field(min_length=1, max_length=64)
    api_port: int = Field(default=8728, ge=1, le=65535)
    api_user: str = Field(default="admin", max_length=100)
    api_pass: str = Field(default="", max_length=256)   # blank = keep existing on edit
    use_ssl: bool = False


class SecretIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)
    profile: str = Field(min_length=1, max_length=120)
    comment: str = Field(default="", max_length=500)


class SecretEditIn(BaseModel):
    password: Optional[str] = Field(default=None, max_length=256)
    profile: Optional[str] = Field(default=None, max_length=120)
    comment: Optional[str] = Field(default=None, max_length=500)
    disabled: Optional[bool] = None


class CopyIn(BaseModel):
    new_name: str = Field(min_length=1, max_length=120)
    new_password: str = Field(min_length=1, max_length=256)


class ChangeProfileIn(BaseModel):
    profile: str = Field(min_length=1, max_length=120)


# ─── Routers CRUD ─────────────────────────────────────────────────────────────

@router.get("/routers")
async def list_routers(s: AsyncSession = Depends(get_session)):
    rows = (await s.scalars(select(MikroRouter).order_by(MikroRouter.name))).all()
    return [_router_dict(r) for r in rows]


@router.post("/routers", status_code=201, dependencies=[Depends(require_admin)])
async def add_router(b: RouterIn, s: AsyncSession = Depends(get_session)):
    if not b.api_pass:
        raise HTTPException(422, "Password is required when adding a router")
    r = MikroRouter(
        name=b.name.strip(), ip=b.ip.strip(),
        api_port=b.api_port, api_user=b.api_user, use_ssl=b.use_ssl,
        api_pass_enc=enc(b.api_pass),
    )
    s.add(r)
    await _commit(s, "Router IP")
    return _router_dict(r)


@router.put("/routers/{rid}", dependencies=[Depends(require_admin)])
async def edit_router(rid: int, b: RouterIn, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    r.name = b.name.strip()
    r.ip = b.ip.strip()
    r.api_port = b.api_port
    r.api_user = b.api_user
    r.use_ssl = b.use_ssl
    if b.api_pass:
        r.api_pass_enc = enc(b.api_pass)
    await _commit(s, "Router")
    return _router_dict(r)


@router.delete("/routers/{rid}", dependencies=[Depends(require_admin)])
async def delete_router(rid: int, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    linked_olts = (await s.scalars(select(OLT).where(OLT.mikrotik_router_id == rid))).all()
    for olt in linked_olts:
        olt.mikrotik_router_id = None
    await s.delete(r)
    await s.commit()
    return {"ok": True}


@router.post("/routers/test", dependencies=[Depends(require_admin)])
async def test_router(b: RouterIn):
    """Test connectivity WITHOUT saving to DB. Returns identity + resource info."""
    client = MikroClient(b.ip.strip(), b.api_user, b.api_pass, b.api_port, b.use_ssl)
    try:
        info = await client.ping_test()
        return {"ok": True, "identity": info.get("identity", ""), "version": info.get("version", ""),
                "board": info.get("board-name", ""), "uptime": info.get("uptime", "")}
    except MikroError as exc:
        return {"ok": False, "detail": str(exc)}


@router.post("/routers/{rid}/check")
async def check_router(rid: int, s: AsyncSession = Depends(get_session)):
    """Probe a saved router and update its status + identity in DB."""
    r = await _get_router(rid, s)
    client = _client(r)
    try:
        info = await client.ping_test()
        r.status = "online"
        r.identity = info.get("identity", "")
        r.ros_version = info.get("version", "")
        r.board = info.get("board-name", "")
        r.uptime = info.get("uptime", "")
        r.last_check_at = utcnow()
    except MikroError as exc:
        r.status = "offline"
        r.last_check_at = utcnow()
        await s.commit()
        return {"ok": False, "detail": str(exc)}
    await s.commit()
    return {"ok": True, **_router_dict(r)}


# ─── PPPoE Profiles (cached from router) ──────────────────────────────────────

@router.get("/routers/{rid}/profiles")
async def pppoe_profiles(rid: int, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    client = _client(r)
    try:
        raw = await client.pppoe_profiles()
    except MikroError as exc:
        raise HTTPException(502, str(exc))

    # Sync cache
    names_from_router = {p.get("name", "") for p in raw if p.get("name")}
    cached = {p.name: p for p in (await s.scalars(select(MikroProfile).where(MikroProfile.router_id == rid))).all()}

    # Add / update
    for p in raw:
        name = p.get("name", "")
        if not name:
            continue
        if name in cached:
            cached[name].rate_limit = p.get("rate-limit", "")
            cached[name].local_address = p.get("local-address", "")
            cached[name].remote_address = p.get("remote-address", "")
        else:
            s.add(MikroProfile(router_id=rid, name=name, rate_limit=p.get("rate-limit", ""),
                               local_address=p.get("local-address", ""), remote_address=p.get("remote-address", "")))
    # Remove stale
    for name, obj in cached.items():
        if name not in names_from_router:
            await s.delete(obj)
    await s.commit()

    return [{"id": p.get(".id"), "name": p.get("name"), "rate_limit": p.get("rate-limit", ""),
             "local_address": p.get("local-address", ""), "remote_address": p.get("remote-address", "")}
            for p in raw]


# ─── PPPoE Secrets (users) ────────────────────────────────────────────────────

@router.get("/routers/{rid}/secrets")
async def list_secrets(rid: int, q: str = "", s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        raw = await _client(r).pppoe_secrets(q)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    names = [x.get("name", "") for x in raw if x.get("name")]
    history = {}
    if names:
        history = {
            item.username: item
            for item in (await s.scalars(
                select(MikroPppoeLastCaller).where(
                    MikroPppoeLastCaller.router_id == rid,
                    MikroPppoeLastCaller.username.in_(names),
                )
            )).all()
        }
    result = []
    for item in raw:
        secret = _fmt_secret(item)
        last_caller = history.get(secret["name"])
        if last_caller:
            secret["caller_id"] = last_caller.mac or secret["caller_id"]
            secret["last_onu"] = _last_onu_dict(last_caller)
        else:
            secret["last_onu"] = None
        result.append(secret)
    return result


@router.post("/routers/{rid}/secrets", status_code=201, dependencies=[Depends(require_admin)])
async def add_secret(rid: int, b: SecretIn, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        result = await _client(r).add_secret(b.name, b.password, b.profile, b.comment)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    return _fmt_secret(result)


@router.patch("/routers/{rid}/secrets/{secret_id}", dependencies=[Depends(require_admin)])
async def edit_secret(rid: int, secret_id: str, b: SecretEditIn, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    fields = {k: v for k, v in {
        "password": b.password, "profile": b.profile,
        "comment": b.comment, "disabled": ("yes" if b.disabled else "no") if b.disabled is not None else None,
    }.items() if v is not None}
    if not fields:
        raise HTTPException(422, "No fields to update")
    try:
        result = await _client(r).edit_secret(secret_id, **fields)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    return _fmt_secret(result)


@router.delete("/routers/{rid}/secrets/{secret_id}", dependencies=[Depends(require_admin)])
async def delete_secret(rid: int, secret_id: str, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        await _client(r).delete_secret(secret_id)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True}


@router.get("/routers/{rid}/secrets/{secret_id}/credentials",
            dependencies=[Depends(require_admin)])
async def secret_credentials(rid: int, secret_id: str, response: Response,
                             s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        secret = await _client(r).get_secret_credentials(secret_id)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return {"username": secret.get("name", ""), "password": secret["password"]}


@router.post("/routers/{rid}/secrets/{secret_id}/copy", status_code=201,
             dependencies=[Depends(require_admin)])
async def copy_secret(rid: int, secret_id: str, b: CopyIn, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        result = await _client(r).copy_secret(secret_id, b.new_name, b.new_password)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    return _fmt_secret(result)


@router.post("/routers/{rid}/secrets/{secret_id}/change-profile",
             dependencies=[Depends(require_admin)])
async def change_profile(rid: int, secret_id: str, b: ChangeProfileIn, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        result = await _client(r).edit_secret(secret_id, profile=b.profile)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    return _fmt_secret(result)


# ─── PPPoE Active Sessions ────────────────────────────────────────────────────

@router.get("/routers/{rid}/active")
async def active_sessions(rid: int, q: str = "", s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        raw = await _client(r).pppoe_active(q)
    except MikroError as exc:
        raise HTTPException(502, str(exc))

    active = [_fmt_active(x) for x in raw]
    macs = {
        re.sub(r"[^0-9A-Fa-f]", "", session["caller_id"]).upper()
        for session in active if session["caller_id"]
    }
    onu_by_mac = {}
    if macs:
        matches = (await s.execute(
            select(OnuMacAddress.mac_compact, ONU)
            .join(ONU, OnuMacAddress.onu_id == ONU.id)
            .where(OnuMacAddress.mac_compact.in_(macs))
        )).all()
        onu_by_mac = {mac: onu for mac, onu in matches}

    names = [session["name"] for session in active if session["name"]]
    history = {}
    if names:
        history = {
            item.username: item
            for item in (await s.scalars(
                select(MikroPppoeLastCaller).where(
                    MikroPppoeLastCaller.router_id == rid,
                    MikroPppoeLastCaller.username.in_(names),
                )
            )).all()
        }

    for session in active:
        caller_id = session["caller_id"]
        if not caller_id or not session["name"]:
            continue
        mac_compact = re.sub(r"[^0-9A-Fa-f]", "", caller_id).upper()
        onu = onu_by_mac.get(mac_compact)
        last_caller = history.get(session["name"])
        if last_caller is None:
            last_caller = MikroPppoeLastCaller(
                router_id=rid, username=session["name"],
            )
            s.add(last_caller)
            history[session["name"]] = last_caller
        previous_mac = last_caller.mac or ""
        last_caller.mac = caller_id
        last_caller.last_seen_at = utcnow()
        if onu:
            last_caller.onu_id = onu.id
            last_caller.onu_sn = onu.sn
            last_caller.onu_name = onu.name
            last_caller.onu_slot = onu.slot
            last_caller.onu_pon = onu.pon
            last_caller.onu_number = onu.onu_number
        elif previous_mac != caller_id:
            previous_compact = re.sub(r"[^0-9A-Fa-f]", "", previous_mac).upper()
            if previous_compact != mac_compact:
                last_caller.onu_id = None
                last_caller.onu_sn = ""
                last_caller.onu_name = ""
                last_caller.onu_slot = None
                last_caller.onu_pon = None
                last_caller.onu_number = None
        session["onu"] = _last_onu_dict(last_caller)
    if history:
        await s.commit()
    return active


@router.delete("/routers/{rid}/active/{active_id}", dependencies=[Depends(require_admin)])
async def disconnect_session(rid: int, active_id: str, s: AsyncSession = Depends(get_session)):
    r = await _get_router(rid, s)
    try:
        await _client(r).disconnect_active(active_id)
    except MikroError as exc:
        raise HTTPException(502, str(exc))
    return {"ok": True}


# ─── MAC lookup: PPPoE user → MAC → ONU/SN on OLT ────────────────────────────

@router.get("/routers/{rid}/mac-lookup")
async def mac_lookup(rid: int, username: str, s: AsyncSession = Depends(get_session)):
    """Find the active or last-seen PPPoE caller ID and cross-reference it with OLT ONUs.

    Steps:
    1. Query MikroTik for the active session, falling back to the saved caller ID.
    2. Search OnuMacAddress table for that MAC → get ONU record and OLT SN.
    """
    r = await _get_router(rid, s)
    try:
        caller_id = await _client(r).active_caller_id(username)
    except MikroError as exc:
        raise HTTPException(502, str(exc))

    last_caller = await s.scalar(
        select(MikroPppoeLastCaller).where(
            MikroPppoeLastCaller.router_id == rid,
            MikroPppoeLastCaller.username == username,
        )
    )
    if not caller_id and last_caller:
        caller_id = last_caller.mac

    if not caller_id:
        return {"username": username, "mac": None, "onu": None,
                "message": "User has no active PPPoE session and no saved caller ID"}

    # Normalise MAC for DB lookup (remove separators, uppercase)
    mac_compact = caller_id.replace(":", "").replace("-", "").upper()

    row = (await s.execute(
        select(OnuMacAddress, ONU)
        .join(ONU, OnuMacAddress.onu_id == ONU.id)
        .where(OnuMacAddress.mac_compact == mac_compact)
        .limit(1)
    )).first()

    onu_info = _last_onu_dict(last_caller) if last_caller and last_caller.mac == caller_id else None
    if row:
        mac_row, onu = row
        onu_info = {
            "onu_id": onu.id, "sn": onu.sn, "name": onu.name,
            "onu_type": onu.onu_type, "status": onu.status,
            "slot": onu.slot, "pon": onu.pon, "onu_number": onu.onu_number,
            "pppoe_user": onu.pppoe_user,
        }

    return {
        "username": username,
        "mac": caller_id,
        "onu": onu_info,
        "message": ("ONU found on OLT" if row else
                    "Showing the last known ONU" if onu_info else "MAC not found in OLT MAC table"),
    }


@router.get("/onus/{onu_id}/wan-ips")
async def onu_wan_ips(onu_id: int, mac: list[str] = Query(default=[], max_length=64),
                      refresh: bool = False,
                      s: AsyncSession = Depends(get_session)):
    """Return WAN IPs from currently active PPPoE sessions matching the ONU's MACs."""
    onu = await s.get(ONU, onu_id)
    if not onu or onu.hidden_duplicate:
        raise HTTPException(404, "ONU not found")
    olt = await s.get(OLT, onu.olt_id)

    if not mac:
        cached_macs = (await s.scalars(
            select(OnuMacAddress).where(OnuMacAddress.onu_id == onu_id)
        )).all() if refresh else []
        mac = [row.mac for row in cached_macs]
        if not mac:
            await s.execute(delete(OnuWanIp).where(OnuWanIp.onu_id == onu_id))
            await s.commit()
            return {
                "matches": [], "errors": [], "active_sessions": 0,
                "message": "No MAC addresses are available to match against PPPoE sessions.",
            }

    macs = {
        re.sub(r"[^0-9A-Fa-f]", "", value).upper()
        for value in mac
        if re.fullmatch(r"(?:[0-9A-Fa-f]{12}|[0-9A-Fa-f]{4}(?:[.:-][0-9A-Fa-f]{4}){2}|"
                        r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})", value.strip())
    }
    if not macs:
        raise HTTPException(422, "No valid MAC addresses were supplied from the ONU's OLT MAC table.")

    routers = (await s.scalars(select(MikroRouter).order_by(MikroRouter.name))).all()
    if olt and olt.vendor == "cdata":
        routers = _routers_for_olt(routers, olt)
        if not routers:
            await s.execute(delete(OnuWanIp).where(OnuWanIp.onu_id == onu_id))
            await s.commit()
            return {
                "matches": [], "errors": [], "active_sessions": 0,
                "message": (f"The linked MikroTik router is unavailable for C-Data OLT '{olt.name}'."
                            if olt.mikrotik_router_id is not None else
                            f"No MikroTik router name matches C-Data OLT '{olt.name}'."),
            }
        if len(routers) > 1:
            names = ", ".join(r.name for r in routers)
            await s.execute(delete(OnuWanIp).where(OnuWanIp.onu_id == onu_id))
            await s.commit()
            return {
                "matches": [], "errors": [f"Multiple MikroTik routers match C-Data OLT '{olt.name}': {names}."],
                "active_sessions": 0, "message": "PPPoE lookup skipped because the router match is ambiguous.",
            }
    if not routers:
        await s.execute(delete(OnuWanIp).where(OnuWanIp.onu_id == onu_id))
        await s.commit()
        return {
            "matches": [], "errors": [], "active_sessions": 0,
            "message": "No MikroTik routers are configured to resolve active PPPoE sessions.",
        }

    async def read_matches(router: MikroRouter) -> tuple[list[dict], str | None, int]:
        try:
            sessions = await _client(router).pppoe_active()
        except MikroError as exc:
            return [], f"{router.name}: {exc}", 0
        matches = []
        for session in sessions:
            if session.get("service", "").casefold() not in ("", "pppoe"):
                continue
            username = session.get("name", "").strip()
            if olt and olt.vendor not in ("cdata", "hioso") and onu.pppoe_user.strip() \
                    and username.casefold() != onu.pppoe_user.strip().casefold():
                continue
            caller_id = session.get("caller-id", "").strip()
            compact = re.sub(r"[^0-9A-Fa-f]", "", caller_id).upper()
            address = session.get("address", "").strip()
            if compact not in macs or not address:
                continue
            try:
                ipaddress.ip_address(address)
            except ValueError:
                continue
            matches.append({
                "router": router.name,
                "username": username,
                "mac": caller_id,
                "mac_compact": compact,
                "ip": address,
                "url": _wan_ip_url(address),
                "uptime": session.get("uptime", ""),
            })
        return matches, None, len(sessions)

    results = await asyncio.gather(*(read_matches(router) for router in routers))
    matches = [match for rows, _, _ in results for match in rows]
    errors = [error for _, error, _ in results if error]
    active_sessions = sum(count for _, _, count in results)
    ambiguous = _has_ambiguous_pppoe_matches(matches)
    if ambiguous:
        matches = []
    else:
        matches = _one_pppoe_match_per_user(matches)

    await s.execute(delete(OnuWanIp).where(OnuWanIp.onu_id == onu_id))
    now = utcnow()
    s.add_all(OnuWanIp(
        onu_id=onu_id, mac=match["mac"], mac_compact=match["mac_compact"],
        router=match["router"][:100], username=match["username"][:100],
        ip=match["ip"], updated_at=now,
    ) for match in matches)
    await s.commit()

    if ambiguous:
        message = "Multiple PPPoE usernames match MACs learned behind this ONU."
    elif matches and errors:
        message = "Updated available PPPoE matches; one or more routers could not be refreshed."
    elif matches:
        message = "Active PPPoE IP matched by a MAC address learned behind this ONU."
    elif errors and not active_sessions:
        message = "Could not check active PPPoE sessions on one or more MikroTik routers."
    elif errors:
        message = (f"Checked {active_sessions} active PPPoE session(s), but none had a caller MAC "
                   "matching a MAC address learned behind this ONU.")
    elif not active_sessions:
        message = "No active PPPoE sessions were found on the configured MikroTik routers."
    else:
        message = (f"Checked {active_sessions} active PPPoE session(s), but none had a caller MAC "
                   "matching a MAC address learned behind this ONU.")
    return {"matches": matches, "errors": errors, "active_sessions": active_sessions,
            "ambiguous": ambiguous, "message": message}


def _wan_ip_url(address: str) -> str:
    parsed = ipaddress.ip_address(address)
    host = f"[{address}]" if parsed.version == 6 else address
    return f"http://{host}/"


# ─── Format helpers ───────────────────────────────────────────────────────────

def _fmt_secret(x: dict) -> dict:
    disabled = str(x.get("disabled", "false")).lower() in {"true", "yes", "1"}
    return {
        "id": x.get(".id", ""), "name": x.get("name", ""),
        "profile": x.get("profile", ""), "service": x.get("service", "pppoe"),
        "comment": x.get("comment", ""), "disabled": disabled,
        "caller_id": x.get("caller-id", ""),
    }


def _fmt_active(x: dict) -> dict:
    radius = str(x.get("radius", "false")).lower() in {"true", "yes", "1"}
    return {
        "id": x.get(".id", ""), "name": x.get("name", ""),
        "service": x.get("service", ""), "caller_id": x.get("caller-id", ""),
        "address": x.get("address", ""), "uptime": x.get("uptime", ""),
        "encoding": x.get("encoding", ""), "session_id": x.get("session-id", ""),
        "radius": radius,
    }


def _last_onu_dict(item: MikroPppoeLastCaller | None) -> dict | None:
    if not item or not any((
        item.onu_id, item.onu_sn, item.onu_name,
        item.onu_slot is not None, item.onu_pon is not None, item.onu_number is not None,
    )):
        return None
    return {
        "onu_id": item.onu_id, "sn": item.onu_sn, "name": item.onu_name,
        "slot": item.onu_slot, "pon": item.onu_pon, "onu_number": item.onu_number,
    }
