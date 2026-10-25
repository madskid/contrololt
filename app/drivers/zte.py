"""ZTE driver (C300 / C320 / C6xx): CLI/OID profiles, per-OLT Telnet session registry, and high-level operations.

NOTE: CLI syntax and OIDs in the profiles below are defaults that have not been verified
against all firmware. They can be overridden without code changes in data/profiles.json
(see README). Raw output is always returned to the UI for easier adaptation.
"""
import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields, replace
from typing import Literal

from app.config import DATA_DIR, settings
from app.drivers import zte_parsers as P
from app.drivers.snmp import SnmpClient, SnmpError, ticks_to_seconds
from app.drivers.telnet import BusyError, TelnetError, TelnetSession  # noqa: F401 (re-export)

log = logging.getLogger("smartolt.zte")

OID_SYSDESCR, OID_UPTIME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.3.0"
OID_IFNAME, OID_IFDESCR = "1.3.6.1.2.1.31.1.1.1.1", "1.3.6.1.2.1.2.2.1.2"


@dataclass(frozen=True)
class Profile:
    olt_if: str
    onu_if: str
    # --- CLI ---
    state_cmd: str = "show gpon onu state"
    state_cmd_port: str = "show gpon onu state {olt_if}"
    rx_cmd_port: str = "show pon power onu-rx {olt_if}"
    detail_cmd: str = "show gpon onu detail-info {onu_if}"
    atten_cmd: str = "show pon power attenuation {onu_if}"
    cfg_cmds: tuple = ("show running-config interface {onu_if}", "show onu running config {onu_if}")   # {vport_if} is available.
    sw_cmds: tuple = ("show gpon remote-onu equip {onu_if}", "show gpon onu detail-info {onu_if}")
    traffic_cmds: tuple = ("show interface {onu_if}",)
    save_cmd: str = "write"
    cards_cmd: str = "show card"
    eth_cmd: str = "show gpon remote-onu interface eth {onu_if}"
    mng_cfg_cmd: str = "show onu running config {onu_if}"
    optical_cmd: str = "show interface optical-module-info {port}"
    diag_cmds: tuple = ("show system-group", "show temperature", "show fan", "show processor", "show memory")
    uncfg_cmd: str = "show gpon onu uncfg"
    uncfg_cmd_alt: str = ""                     # Try if the primary command is rejected or returns no rows.
    mac_cmd: str = "show mac gpon onu {onu_if}"
    onu_type_cmd: str = "show onu-type gpon"
    # --- SNMP (tabel berindeks ifIndex.onuId) ---
    oid_state: str = "1.3.6.1.4.1.3902.1012.3.28.2.1.4"
    oid_rx: str = "1.3.6.1.4.1.3902.1012.3.50.12.1.1.10"
    oid_olt_rx: str = "1.3.6.1.4.1.3902.1012.3.50.12.1.1.14"
    rx_scale: float = 0.002
    rx_offset: float = -30.0
    rx_invalid: tuple = (65535, 32767, 0)
    # --- Registration templates (CLI-family-specific; provision.py validates every line against the allowlist). ---
    vport_if: str = ""                          # Empty = service port is configured under the ONU interface (C300/C320).
    reg_tcont: str = "tcont 1 name {svc} profile {ul}"
    reg_gemport: str = "gemport 1 name {svc} tcont 1"
    reg_dl_limit: str = "gemport 1 traffic-limit downstream {dl}"     # Empty = not sent.
    reg_encrypt: str = "encrypt 1 enable downstream"
    reg_hybrid: str = "switchport mode hybrid vport 1"                # Empty = not sent.
    reg_service_port: str = "service-port 1 vport 1 user-vlan {vlan} vlan {vlan}"
    reg_service: str = "service {svc} gemport 1 vlan {vlan}"          # `service` line in pon-onu-mng.
    reg_eth_bridge: str = "vlan port eth_0/{i} mode hybrid def-vlan {vlan}"
    reg_veip_bridge: str = "vlan port veip_1 mode hybrid"
    reg_web_mgmt: str = "security-mgmt 212 state enable mode forward protocol web"
    reg_routing_filter: str = "vlan-filter"
    reg_routing_priority_keyword: str = "pri"
    reg_routing_vlan_keyword: str = "vlan"
    reg_routing_service: str = ""
    reg_pppoe_wan: str = ""
    pppoe_vlan_profile: str = "PPPoE"
    vlan_db: bool = True                        # True = buat VLAN lewat `vlan database`
    tcont_list_cmd: str = "show gpon profile tcont"       # List T-CONT/traffic profiles (checked before registration).
    traffic_list_cmd: str = "show gpon profile traffic"


PROFILES = {
    # C300 / C320: gpon-olt_1/2/3  gpon-onu_1/2/3:4
    "legacy": Profile(olt_if="gpon-olt_{shelf}/{slot}/{pon}", onu_if="gpon-onu_{shelf}/{slot}/{pon}:{onu}"),
    # C600 / C620 / C650: gpon_olt-1/2/3  gpon_onu-1/2/3:4
    # Syntax follows the "ZXA10 C600/C650/C680 Initial configuration" (R1.0) guide: service-port is under the vport interface,
    # T-CONT/GEM port have no names, `service HSI gemport 1 cos 0 vlan N`, `vlan port eth_0/1 mode tag vlan N pri 0`,
    # and unconfigured ONUs use `show pon onu uncfg`. C300-specific SNMP OIDs do not apply to C6xx, so they are empty
    # (status/signal use CLI) until configured in data/profiles.json.
    "c6xx": Profile(olt_if="gpon_olt-{shelf}/{slot}/{pon}", onu_if="gpon_onu-{shelf}/{slot}/{pon}:{onu}",
                    vport_if="vport-{shelf}/{slot}/{pon}.{onu}:{vport}",
                    uncfg_cmd="show pon onu uncfg", uncfg_cmd_alt="show gpon onu uncfg",
                    cfg_cmds=("show running-config interface {onu_if}", "show running-config interface {vport_if}",
                              "show onu running config {onu_if}"),
                    mac_cmd="show mac interface {vport_if}",
                    oid_state="", oid_rx="", oid_olt_rx="",
                    reg_tcont="tcont 1 profile {ul}", reg_gemport="gemport 1 tcont 1", reg_dl_limit="", reg_hybrid="",
                    reg_service_port="service-port 1 user-vlan {vlan} vlan {vlan}",
                    reg_service="service {svc} gemport 1 cos 0 vlan {vlan}",
                    reg_eth_bridge="vlan port eth_0/{i} mode tag vlan {vlan} pri 0", reg_routing_filter="vid-filter",
                    reg_routing_priority_keyword="priority", reg_routing_vlan_keyword="vid",
                    reg_routing_service="service {svc} gemport 1 vlan {vlan}",
                    reg_pppoe_wan="wan-ip ipv4 mode pppoe username {user} password {pw} vlan-profile {vlan_profile} host 1",
                    vlan_db=False),
}

MODEL_PRESETS = [
    {"key": "C300", "label": "ZTE C300", "family": "legacy"},
    {"key": "C320", "label": "ZTE C320", "family": "legacy"},
    {"key": "C600", "label": "ZTE C600", "family": "c6xx"},
    {"key": "C620", "label": "ZTE C620", "family": "c6xx"},
    {"key": "C650", "label": "ZTE C650", "family": "c6xx"},
    {"key": "C680", "label": "ZTE C680", "family": "c6xx"},
]
FAMILY_LABELS = {"legacy": "C300/C320 style  (gpon-olt_1/1/1)", "c6xx": "C6xx style  (gpon_olt-1/1/1)"}


def _overrides() -> dict:
    path = DATA_DIR / "profiles.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.error("Invalid profiles.json; ignoring it: %s", exc)
        return {}


def profile_for(family: str) -> Profile:
    base = PROFILES.get(family, PROFILES["legacy"])
    ov = _overrides().get(family, {})
    names = {f.name for f in fields(Profile)}
    clean = {k: (tuple(v) if isinstance(v, list) else v) for k, v in ov.items() if k in names}
    return replace(base, **clean) if clean else base


def family_for_model(model: str) -> str | None:
    for p in MODEL_PRESETS:
        if p["key"].lower() == model.strip().lower():
            return p["family"]
    return None


@dataclass
class OltConn:
    id: int
    name: str
    ip: str
    telnet_port: int
    telnet_user: str
    telnet_pass: str
    enable_pass: str
    snmp_community: str
    snmp_port: int
    shelf: int
    family: str

    @property
    def sig(self) -> tuple:
        return (self.ip, self.telnet_port, self.telnet_user, self.telnet_pass, self.enable_pass)

    @property
    def profile(self) -> Profile:
        return profile_for(self.family)

    def snmp(self) -> SnmpClient:
        return SnmpClient(self.ip, self.snmp_community, self.snmp_port)


def conn_from_row(olt) -> OltConn:
    from app.security import dec
    return OltConn(id=olt.id, name=olt.name, ip=olt.ip, telnet_port=olt.telnet_port,
                   telnet_user=olt.telnet_user, telnet_pass=dec(olt.telnet_pass_enc),
                   enable_pass=dec(olt.enable_pass_enc), snmp_community=dec(olt.snmp_community_enc),
                   snmp_port=olt.snmp_port, shelf=olt.shelf, family=olt.cli_family)


Key = tuple  # (shelf, slot, pon, onu)


def olt_if(conn: OltConn, shelf: int, slot: int, pon: int) -> str:
    return conn.profile.olt_if.format(shelf=shelf, slot=slot, pon=pon)


def onu_if(conn: OltConn, key: Key) -> str:
    shelf, slot, pon, onu = key
    return conn.profile.onu_if.format(shelf=shelf, slot=slot, pon=pon, onu=onu)


def onu_command_format(conn: OltConn, key: Key) -> dict[str, str]:
    shelf, slot, pon, onu = key
    prof = conn.profile
    return {"onu_if": onu_if(conn, key),
            "vport_if": vport_if(prof, shelf, slot, pon, onu)}


def vport_if(prof: Profile, shelf: int, slot: int, pon: int, onu: int, vport: int = 1) -> str:
    """Return the vport interface name (C6xx family only); empty for C300/C320."""
    return prof.vport_if.format(shelf=shelf, slot=slot, pon=pon, onu=onu, vport=vport) if prof.vport_if else ""


# ───────────────────────── sesi telnet per-OLT ─────────────────────────
class _Link:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tel: TelnetSession | None = None
        self.sig: tuple | None = None
        self.last_used = 0.0


class LinkRegistry:
    """Persistent Telnet sessions per OLT and workload lane, with a global concurrency limit."""

    def __init__(self) -> None:
        self._links: dict[tuple[int, str], _Link] = {}
        self._sem: asyncio.Semaphore | None = None

    def reset(self) -> None:
        self._links.clear()
        self._sem = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(settings.max_olt_concurrency)
        return self._sem

    @asynccontextmanager
    async def session(self, conn: OltConn, wait: float = 60,
                      lane: Literal["config", "poll", "check", "sync"] = "config"):
        link = self._links.setdefault((conn.id, lane), _Link())
        try:
            await asyncio.wait_for(link.lock.acquire(), wait)
        except asyncio.TimeoutError:
            raise BusyError("The OLT is busy with another operation; try again shortly")
        try:
            async with self._semaphore():
                if link.tel and (link.tel.closed or link.sig != conn.sig):
                    await link.tel.close()
                    link.tel = None
                if link.tel is None:
                    tel = TelnetSession(conn.ip, conn.telnet_port, settings.telnet_timeout)
                    try:
                        await tel.login(conn.telnet_user, conn.telnet_pass, conn.enable_pass)
                    except BaseException:
                        await tel.close()
                        raise
                    link.tel, link.sig = tel, conn.sig
                try:
                    yield link.tel
                except BaseException:
                    # The session may be in a partial state; close it and log in again next time.
                    await link.tel.close()
                    link.tel = None
                    raise
        finally:
            link.last_used = time.monotonic()
            link.lock.release()

    async def close_all(self) -> None:
        for olt_id in {key[0] for key in self._links}:
            await self.drop(olt_id)

    async def drop(self, olt_id: int) -> None:
        keys = [key for key in self._links if key[0] == olt_id]
        for key in keys:
            link = self._links.pop(key, None)
            if link and link.tel:
                await link.tel.close()

    async def janitor(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for key, link in list(self._links.items()):
                if link.tel and not link.lock.locked() and now - link.last_used > settings.telnet_idle_close:
                    async with link.lock:
                        if (self._links.get(key) is link and link.tel
                                and time.monotonic() - link.last_used > settings.telnet_idle_close):
                            await link.tel.close()
                            link.tel = None


links = LinkRegistry()


# ───────────────────────── operasi ─────────────────────────
async def snmp_info(conn: OltConn) -> dict:
    """Read sysDescr and uptime via SNMP. Raises SnmpError on failure."""
    vals = await conn.snmp().get(OID_SYSDESCR, OID_UPTIME)
    descr = next((v for k, v in vals.items() if k.endswith(OID_SYSDESCR)), "")
    uptime = next((v for k, v in vals.items() if k.endswith(OID_UPTIME)), "")
    return {"sys_descr": descr[:300], "uptime_s": ticks_to_seconds(uptime)}


async def probe(conn: OltConn) -> dict:
    """Test SNMP and Telnet separately; failure of one does not fail the other."""
    res = {"snmp_ok": False, "snmp_msg": "", "telnet_ok": False, "telnet_msg": "",
           "sys_descr": "", "uptime_s": None}
    try:
        res.update(await snmp_info(conn))
        res["snmp_ok"], res["snmp_msg"] = True, "SNMP OK"
    except SnmpError as exc:
        res["snmp_msg"] = str(exc)
    try:
        if conn.id < 0:  # Test before the OLT is saved: use a one-off session, not the registry.
            tel = TelnetSession(conn.ip, conn.telnet_port, settings.telnet_timeout)
            try:
                await tel.login(conn.telnet_user, conn.telnet_pass, conn.enable_pass)
            finally:
                await tel.close()
        else:
            async with links.session(conn, wait=20, lane="check"):
                pass
        res["telnet_ok"], res["telnet_msg"] = True, "Telnet login OK"
    except (TelnetError, BusyError, OSError, asyncio.TimeoutError) as exc:
        res["telnet_msg"] = str(exc) or exc.__class__.__name__
    return res


@dataclass
class ConfigRead:
    onus: list[dict]
    states: dict[Key, str] = field(default_factory=dict)
    complete: bool = True
    cfg_bytes: int = 0
    extras: dict = field(default_factory=dict)     # PON / uplinks / VLANs from running-config.
    cards: dict | None = None                       # {"rows": [...], "raw": "..."} when `show card` is parsed.


async def _read_types(cli, prof: Profile) -> dict:
    """Read ONU types known to the OLT (`show onu-type gpon`). Rejected command or unknown format returns no types."""
    if not prof.onu_type_cmd:
        return {"ok": False, "types": {}, "raw": ""}
    r = await cli.run(prof.onu_type_cmd, overall=30, idle=10)
    types = P.parse_onu_types(r.text) if not r.error else {}
    return {"ok": not r.error, "types": types, "raw": r.text, "command": prof.onu_type_cmd}


async def read_onu_types(conn: OltConn) -> dict:
    async with links.session(conn, wait=30, lane="check") as cli:
        return await _read_types(cli, conn.profile)


async def read_config_and_states(conn: OltConn) -> ConfigRead:
    prof = conn.profile
    async with links.session(conn, wait=300, lane="sync") as cli:
        cfg = await cli.run("show running-config", overall=900, idle=45)
        if cfg.error or not cfg.text.strip():
            raise TelnetError("show running-config failed: " + (cfg.text.strip()[:120] or "empty output"))
        onus = P.parse_running_config(cfg.text)
        extras = P.parse_olt_config_extras(cfg.text)
        cards = None
        try:
            cr = await cli.run(prof.cards_cmd, overall=30, idle=10)
            if not cr.error:
                cards = {"rows": P.parse_cards(cr.text), "raw": cr.text}
        except TelnetError:
            raise
        except Exception:
            pass
        live = {"types": {}}
        try:
            live = await _read_types(cli, prof)
        except TelnetError:
            raise
        except Exception:
            pass
        for name, t in live["types"].items():          # OLT types complement those found in running-config.
            extras.setdefault("onu_types", {}).setdefault(name, t)
        extras["onu_types_live"] = bool(live["types"])
        states: dict[Key, str] = {}
        st = await cli.run(prof.state_cmd, overall=180, idle=30)
        if st.complete and not st.error:
            states = P.parse_onu_state(st.text)
    return ConfigRead(onus=onus, states=states, complete=cfg.complete, cfg_bytes=len(cfg.text),
                      extras=extras, cards=cards)


_IFMAP: dict[int, tuple[float, dict]] = {}


async def snmp_ifmap(conn: OltConn, max_age: float = 3600) -> dict[int, tuple]:
    """ifIndex -> (shelf, slot, pon). Found through ifName/ifDescr, never guessed from a formula."""
    cached = _IFMAP.get(conn.id)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    snmp = conn.snmp()
    for oid in (OID_IFNAME, OID_IFDESCR):
        rows = await snmp.walk(oid, total=60)
        m = {}
        for idx, name in rows.items():
            mo = P.OLT_IF_RE.fullmatch(name.strip())
            if mo and idx.isdigit():
                m[int(idx)] = tuple(int(x) for x in mo.groups())
        if m:
            _IFMAP[conn.id] = (time.monotonic(), m)
            return m
    raise SnmpError("gpon-olt interface not found in ifName/ifDescr")


def _rx_dbm(raw: str, prof: Profile) -> float | None:
    try:
        v = int(float(raw))
    except ValueError:
        return None
    if v in prof.rx_invalid:
        return None
    dbm = v * prof.rx_scale + prof.rx_offset
    return round(dbm, 2) if -60 < dbm < 10 else None


def _snmp_key(suffix: str, ifmap: dict) -> Key | None:
    parts = suffix.split(".")
    if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    loc = ifmap.get(int(parts[0]))
    return (*loc, int(parts[1])) if loc else None


async def poll_snmp(conn: OltConn) -> tuple[dict[Key, tuple[str, str]], dict[Key, tuple]]:
    """Read status and signal for all ONUs via SNMP bulkwalk. Return (states, rx)."""
    prof, snmp = conn.profile, conn.snmp()
    if not prof.oid_state:
        raise SnmpError("ONU status OID is not configured for this CLI family -- using CLI (set it in data/profiles.json)")
    ifmap = await snmp_ifmap(conn)
    rows = await snmp.walk(prof.oid_state)
    if not rows:
        raise SnmpError("ONU status table is empty -- the profile OID may not match")
    states, unmapped = {}, 0
    for suffix, val in rows.items():
        key = _snmp_key(suffix, ifmap)
        if key is None:
            unmapped += 1
            continue
        num = P.snmp_int(val)          # '4', 'INTEGER: 4' atau 'working(4)' -> 4
        phase = P.SNMP_PHASE.get(num, val) if num is not None else val
        states[key] = (P.phase_to_status(phase), phase)
    if unmapped:
        _IFMAP.pop(conn.id, None)  # ifIndex map may be stale; walk again on the next poll.
    rx: dict[Key, tuple] = {}
    try:
        onu_rx = await snmp.walk(prof.oid_rx)
        olt_rx = await snmp.walk(prof.oid_olt_rx) if prof.oid_olt_rx else {}
        for suffix, val in onu_rx.items():
            key = _snmp_key(suffix, ifmap)
            if key:
                rx[key] = (_rx_dbm(val, prof), None)
        for suffix, val in olt_rx.items():
            key = _snmp_key(suffix, ifmap)
            if key:
                rx[key] = (rx.get(key, (None, None))[0], _rx_dbm(val, prof))
    except SnmpError as exc:
        log.info("SNMP signal levels unavailable on %s: %s", conn.name, exc)
    return states, rx


async def poll_cli_states(conn: OltConn, ports: list[tuple] | None = None) -> dict[Key, tuple[str, str]]:
    """Read status for all ONUs via CLI. If `show gpon onu state` without arguments is rejected/empty (some C6xx
    firmware requires a port argument), retry per PON from `ports` [(shelf, slot, pon)]."""
    prof = conn.profile
    async with links.session(conn, wait=60, lane="poll") as cli:
        r = await cli.run(prof.state_cmd, overall=180, idle=30)
        if not r.complete:
            raise TelnetError("Global ONU status command did not complete (OLT prompt not received)")
        raw = P.parse_onu_state(r.text) if not r.error else {}
        if not raw and ports:
            for shelf, slot, pon in ports:
                rp = await cli.run(prof.state_cmd_port.format(olt_if=olt_if(conn, shelf, slot, pon)), overall=60, idle=20)
                if not rp.complete:
                    raise TelnetError(
                        f"ONU status command for PON {shelf}/{slot}/{pon} did not complete (OLT prompt not received)"
                    )
                if not rp.error:
                    raw.update(P.parse_onu_state(rp.text))
        if not raw and r.error:
            raise TelnetError("show gpon onu state was rejected: " + r.text[:100])
    return {k: (P.phase_to_status(v), v) for k, v in raw.items()}


async def poll_cli_rx(conn: OltConn, ports: list[tuple]) -> dict[Key, tuple]:
    prof, out = conn.profile, {}
    async with links.session(conn, wait=60, lane="poll") as cli:
        for shelf, slot, pon in ports:
            r = await cli.run(prof.rx_cmd_port.format(olt_if=olt_if(conn, shelf, slot, pon)), overall=60, idle=20)
            if not r.error:
                out.update({k: (v, None) for k, v in P.parse_onu_rx_list(r.text).items()})
    return out


async def onu_status(conn: OltConn, key: Key, include_mac: bool = True) -> dict:
    prof, fmt = conn.profile, onu_command_format(conn, key)
    async with links.session(conn, wait=30, lane="check") as cli:
        d = await cli.run(prof.detail_cmd.format(**fmt), overall=30, idle=15)
        a = await cli.run(prof.atten_cmd.format(**fmt), overall=30, idle=15)
        m = None
        if include_mac and prof.mac_cmd:      # MAC on the ONU and connected devices; failure must not fail Get status.
            try:
                m = await cli.run(prof.mac_cmd.format(**fmt), overall=30, idle=15)
            except (TelnetError, asyncio.TimeoutError):
                raise
            except Exception:
                m = None
    fields_ = {k: P.format_mac(v) for k, v in P.parse_kv(d.text).items()}   # nilai berupa MAC -> XX:XX:XX:XX:XX:XX
    phase = next((v for k, v in fields_.items() if "phase" in k.lower()), "")
    rows = P.parse_mac_table(m.text) if m is not None and not m.error else []
    return {"interface": fmt["onu_if"], "phase_state": phase,
            "status": P.phase_to_status(phase) if phase else "unknown",
            "fields": fields_, "optical": P.parse_power_attenuation(a.text), "history": P.parse_onu_history(d.text)[-10:],
            "mac": {"ok": m is not None and not m.error, "total": len(rows), "rows": rows,
                    "command": prof.mac_cmd.format(**fmt) if prof.mac_cmd else "", "raw": m.text if m is not None else ""},
            "raw": {"detail": d.text, "attenuation": a.text},
            "error": d.error and a.error}


async def read_onu_mac(conn: OltConn, key: Key) -> dict:
    prof = conn.profile
    if not prof.mac_cmd:
        return {"ok": False, "total": 0, "rows": [], "command": "", "raw": ""}
    command = prof.mac_cmd.format(**onu_command_format(conn, key))
    async with links.session(conn, wait=30, lane="check") as cli:
        result = await cli.run(command, overall=30, idle=15)
    rows = P.parse_mac_table(result.text) if not result.error else []
    return {"ok": not result.error, "total": len(rows), "rows": rows,
            "command": command, "raw": result.text}


async def onu_text(conn: OltConn, key: Key, kind: str) -> dict:
    """config: combine `show running-config interface` and `show onu running config` (interface and pon-onu-mng sections).
    sw: first successful command."""
    prof = conn.profile
    cmds = prof.cfg_cmds if kind == "config" else prof.sw_cmds
    done, outs, last = [], [], None
    async with links.session(conn, wait=30, lane="check") as cli:
        for tpl in cmds:
            cmd = tpl.format(onu_if=onu_if(conn, key), vport_if=vport_if(prof, *key))
            r = await cli.run(cmd, overall=30, idle=15)
            last = (cmd, r)
            if not r.error and r.text.strip():
                done.append(cmd)
                outs.append(r.text.strip("\n"))
                if kind != "config":
                    break
    if done:
        return {"command": " ; ".join(done), "text": "\n".join(outs), "ok": True}
    return {"command": last[0], "text": last[1].text, "ok": False}


async def read_onu_cfg(conn: OltConn, key: Key) -> str:
    return (await onu_text(conn, key, "config"))["text"]


async def read_eth(conn: OltConn, key: Key) -> dict:
    """ONU Ethernet port status and per-port VLAN mode (from `vlan port` in pon-onu-mng configuration)."""
    async with links.session(conn, wait=30, lane="check") as cli:
        r = await cli.run(conn.profile.eth_cmd.format(onu_if=onu_if(conn, key)), overall=30, idle=15)
        c = await cli.run(conn.profile.mng_cfg_cmd.format(onu_if=onu_if(conn, key)), overall=30, idle=15)
    modes = P.parse_eth_modes(c.text) if not c.error else {}
    ports = P.parse_eth_ports(r.text) if not r.error else []
    for p_ in ports:
        m = modes.get(p_["port"], {"mode": "LAN", "vlan": ""})
        p_["mode"], p_["vlan"] = m["mode"], m["vlan"]
    return {"ports": ports, "raw": r.text, "ok": not r.error}


async def read_diag(conn: OltConn) -> list[dict]:
    """Raw output from OLT health commands (unverified format -> displayed as-is)."""
    out = []
    async with links.session(conn, wait=30, lane="check") as cli:
        for cmd in conn.profile.diag_cmds:
            r = await cli.run(cmd, overall=20, idle=10)
            out.append({"command": cmd, "ok": not r.error, "output": r.text})
    return out


async def read_optical(conn: OltConn, ports: list[str]) -> dict[str, dict]:
    """Uplink optical module Rx/Tx/temperature/wavelength (`show interface optical-module-info`). Copper ports are skipped."""
    res = {}
    async with links.session(conn, wait=30, lane="check") as cli:
        for port in ports:
            r = await cli.run(conn.profile.optical_cmd.format(port=port), overall=15, idle=8)
            if not r.error:
                d = P.parse_optical_module(r.text)
                if any(v is not None for v in d.values()):
                    res[port] = d
    return res


_TRAFFIC_CACHE: dict[tuple, tuple[float, dict]] = {}


async def onu_traffic(conn: OltConn, key: Key) -> dict:
    ck = (conn.id, key)
    hit = _TRAFFIC_CACHE.get(ck)
    if hit and time.monotonic() - hit[0] < 2:  # Multiple open tabs -> one command every 2 seconds.
        return hit[1]
    prof = conn.profile
    last = None
    async with links.session(conn, wait=15, lane="check") as cli:
        for tpl in prof.traffic_cmds:
            cmd = tpl.format(onu_if=onu_if(conn, key))
            r = await cli.run(cmd, overall=15, idle=8)
            last = (cmd, r)
            if not r.error and r.text.strip():
                break
    cmd, r = last
    res = {"ts": time.time(), "command": cmd, "parsed": P.parse_traffic(r.text) if not r.error else None,
           "raw": r.text, "ok": not r.error}
    _TRAFFIC_CACHE[ck] = (time.monotonic(), res)
    if len(_TRAFFIC_CACHE) > 2000:
        _TRAFFIC_CACHE.clear()
    return res


async def save_config(conn: OltConn) -> tuple[bool, str]:
    prof = conn.profile
    async with links.session(conn, wait=120, lane="config") as cli:
        r = await cli.run(prof.save_cmd, overall=180, idle=90)
    ok = r.complete and not r.error
    return ok, (r.text.strip().replace("\n", " ")[:250] or "OK")


async def read_cards(conn: OltConn) -> dict:
    async with links.session(conn, wait=30, lane="check") as cli:
        r = await cli.run(conn.profile.cards_cmd, overall=30, idle=10)
    return {"rows": P.parse_cards(r.text) if not r.error else [], "raw": r.text, "ok": not r.error}


async def scan_uncfg(conn: OltConn) -> dict:
    async with links.session(conn, wait=180, lane="check") as cli:
        prof = conn.profile
        r = await cli.run(prof.uncfg_cmd, overall=60, idle=15)
        rows = P.parse_uncfg(r.text) if not r.error else []
        if (r.error or not rows) and prof.uncfg_cmd_alt:      # Other firmware may use a different command name.
            r2 = await cli.run(prof.uncfg_cmd_alt, overall=60, idle=15)
            rows2 = P.parse_uncfg(r2.text) if not r2.error else []
            if rows2 or (r.error and not r2.error):
                r, rows = r2, rows2
    return {"rows": rows, "raw": r.text, "ok": not r.error}


IF_OIDS = {"admin": "1.3.6.1.2.1.2.2.1.7", "oper": "1.3.6.1.2.1.2.2.1.8", "mtu": "1.3.6.1.2.1.2.2.1.4",
           "speed": "1.3.6.1.2.1.31.1.1.1.15", "in": "1.3.6.1.2.1.31.1.1.1.6", "out": "1.3.6.1.2.1.31.1.1.1.10"}


def _int(v) -> int | None:
    v = str(v).strip().lower()
    word = {"up": 1, "down": 2, "testing": 3}
    if v in word:
        return word[v]
    m = re.search(r"-?\d+", v)
    return int(m.group()) if m else None


async def snmp_if_table(conn: OltConn, with_counters: bool = False) -> dict[str, dict]:
    """Standard IF-MIB table (not ZTE-specific OIDs): status, MTU, speed, and optional 64-bit counters."""
    snmp = conn.snmp()
    names = await snmp.walk(OID_IFNAME, total=60)
    keys = ["admin", "oper", "mtu", "speed"] + (["in", "out"] if with_counters else [])
    tables = await asyncio.gather(*(snmp.walk(IF_OIDS[k], total=60) for k in keys), return_exceptions=True)
    out = {}
    for idx, name in names.items():
        if not idx.isdigit():
            continue
        row = {"ifindex": int(idx)}
        for k, tab in zip(keys, tables):
            row[k] = _int(tab[idx]) if isinstance(tab, dict) and idx in tab else None
        out[name.strip()] = row
    return out
