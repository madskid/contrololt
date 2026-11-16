"""Read-only SNMP monitoring for the verified C-Data EPON family."""
import asyncio
import re

from app.drivers.snmp import (SnmpClient, SnmpError, onu_position_from_ifname,
                              read_bridge_mac_table, ticks_to_seconds)
from app.drivers.telnet import TelnetSession
from app.security import dec

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
ENTERPRISE = "1.3.6.1.4.1.17409"
ONU_INFO = f"{ENTERPRISE}.2.3.4.1.1"
ONU_NAME = f"{ONU_INFO}.2"
ONU_MAC = f"{ONU_INFO}.7"
ONU_STATE = f"{ONU_INFO}.8"
ONU_RX = f"{ENTERPRISE}.2.3.4.2.1.4"


def client_from_row(olt) -> SnmpClient:
    return SnmpClient(olt.ip, dec(olt.snmp_community_enc), olt.snmp_port, timeout=5, retries=2)


async def test_telnet(host: str, port: int, username: str, password: str, enable_password: str = "") -> str:
    client = TelnetSession(host, port, connect_timeout=15)
    try:
        await client.login(username, password, enable_password)
        return "Telnet login OK."
    finally:
        await client.close()


def _value(values: dict[str, str], oid: str) -> str:
    return next((value for key, value in values.items() if key.endswith(oid)), "")


def _typed_value(value: str) -> str:
    return value.split(":", 1)[-1].strip().strip('"')


def _device_index(value: str) -> tuple[int, int, int] | None:
    try:
        index = int(value.split(".", 1)[0])
    except (TypeError, ValueError):
        return None
    slot = (index >> 24) & 0xFF
    encoded_port = (index >> 8) & 0xFF
    port, number = encoded_port // 0x10 + 1, index & 0xFF
    if not 1 <= slot <= 16 or encoded_port % 0x10 or not 1 <= port <= 16 or not 1 <= number <= 255:
        return None
    return slot, port, number


def _mac(value: str) -> str:
    raw = value.split(":", 1)[-1]
    compact = re.sub(r"[^0-9a-fA-F]", "", raw)
    if not re.fullmatch(r"[0-9a-fA-F]{12}", compact):
        return ""
    return ":".join(compact[i:i + 2].upper() for i in range(0, 12, 2))


def _indexed(table: dict[str, str]) -> dict[int, str]:
    result = {}
    for suffix, value in table.items():
        match = re.match(r"(\d+)(?:\.\d+)*$", suffix)
        if match:
            index = int(match.group(1))
            result[index] = value
    return result


async def probe(host: str, community: str, port: int) -> dict:
    client = SnmpClient(host, community, port, timeout=5, retries=1)
    result = {"snmp_ok": False, "snmp_msg": "", "telnet_ok": False,
              "telnet_msg": "Telnet credentials not supplied; CLI actions remain unavailable.",
              "sys_descr": "", "uptime_s": None, "sys_object_id": ""}
    try:
        values = await client.get(SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME)
        object_id = _value(values, SYS_OBJECT_ID).lstrip(".")
        result.update(
            sys_descr=_value(values, SYS_DESCR)[:300],
            sys_object_id=object_id,
            uptime_s=ticks_to_seconds(_value(values, SYS_UPTIME)),
        )
        if object_id != ENTERPRISE and not object_id.startswith(ENTERPRISE + "."):
            result["snmp_msg"] = "SNMP responded, but sysObjectID did not identify C-Data enterprise 17409."
            return result
        onus = await read_onus(client)
        if not onus:
            result["snmp_msg"] = "C-Data responded, but the EPON ONU tables returned no valid ONU MAC addresses."
            return result
        result["snmp_ok"] = True
        result["snmp_msg"] = f"SNMP OK — C-Data EPON detected ({len(onus)} ONU rows)."
    except SnmpError as exc:
        result["snmp_msg"] = str(exc)
    return result


async def system_info(client: SnmpClient) -> dict:
    values = await client.get(SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME)
    object_id = _value(values, SYS_OBJECT_ID).lstrip(".")
    if object_id != ENTERPRISE and not object_id.startswith(ENTERPRISE + "."):
        raise SnmpError("The SNMP device did not identify as C-Data enterprise 17409.")
    return {
        "sys_descr": _value(values, SYS_DESCR)[:300],
        "sys_object_id": _value(values, SYS_OBJECT_ID),
        "uptime_s": ticks_to_seconds(_value(values, SYS_UPTIME)),
    }


async def read_onus(client: SnmpClient) -> dict[tuple[int, int, int], dict[str, str]]:
    names, macs, states, optical = await asyncio.gather(
        client.walk(ONU_NAME, max_rep=25, total=90),
        client.walk(ONU_MAC, max_rep=25, total=90),
        client.walk(ONU_STATE, max_rep=25, total=90),
        client.walk(ONU_RX, max_rep=25, total=90),
    )
    mac_rows = _indexed(macs)
    state_rows = _indexed(states)
    rx_rows = _indexed(optical)
    name_rows = _indexed(names)
    onus: dict[tuple[int, int, int], dict[str, str]] = {}
    for index, mac_value in mac_rows.items():
        position = _device_index(str(index))
        mac = _mac(mac_value)
        if not position or not mac:
            continue
        name = _typed_value(name_rows.get(index, ""))
        raw_rx = _typed_value(rx_rows.get(index, ""))
        try:
            rx = str(float(raw_rx) / 100) if raw_rx else ""
        except ValueError:
            rx = ""
        onus[position] = {
            "name": name if name.casefold() not in ("na", "unknown") else "",
            "mac": mac.replace(":", ""),
            "state": _typed_value(state_rows.get(index, "")),
            "rx": rx,
        }
    return onus


async def read_onu_macs(
    client: SnmpClient, onus: dict[tuple[int, int, int], dict[str, str]]
) -> dict[tuple[int, int, int], list[str]]:
    positions: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for key in onus:
        positions.setdefault((key[1], key[2]), []).append(key)
    learned: dict[tuple[int, int, int], set[str]] = {}
    for interface, mac in await read_bridge_mac_table(client):
        position = onu_position_from_ifname(interface)
        matches = positions.get(position, []) if position else []
        if len(matches) == 1:
            learned.setdefault(matches[0], set()).add(mac)
    return {key: sorted(macs) for key, macs in learned.items()}
