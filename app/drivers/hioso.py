"""SNMP monitoring, CLI MAC lookup, and narrowly scoped ONU actions for HiOSO HA7308C OLTs."""
import asyncio
import re
from collections import defaultdict

from app.drivers.snmp import SnmpClient, SnmpError, onu_position_from_ifname, ticks_to_seconds
from app.drivers.telnet import TelnetError, TelnetSession
from app.security import dec

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
FIRMWARE = "1.3.6.1.4.1.25355.3.1.8.1.1.2.1"
FW_VERSION = "1.3.6.1.4.1.25355.3.1.13.1.1.2.1"
ONU_NAME = "1.3.6.1.4.1.25355.3.2.6.3.2.1.37.1"
ONU_MAC = "1.3.6.1.4.1.25355.3.2.6.3.2.1.11.1"
ONU_RX = "1.3.6.1.4.1.25355.3.2.6.14.2.1.8.1"
ONU_STATE = "1.3.6.1.4.1.25355.3.2.6.3.2.1.39.1"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_IN = "1.3.6.1.2.1.31.1.1.1.6"
IF_OUT = "1.3.6.1.2.1.31.1.1.1.10"
INDEX_RE = re.compile(r"(\d+)\.(\d+)$")
MAC_TABLE_ROW_RE = re.compile(
    r"^\s*((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s+\d+\s+(epon\s+0/\d+/\d+|epon0/\d+/\d+|\S+)\s+dynamic\s*$",
    re.IGNORECASE,
)
DETAIL_FIELDS = {
    "onuid": "ONU ID",
    "onumac": "MAC Address",
    "onuname": "Name",
    "onlinestatus": "Online Status",
    "activatestatus": "Activate Status",
    "firmwareversion": "Firmware Version",
    "chipid": "Chip ID",
    "modelstring": "Model",
    "onutype": "ONU Type",
    "genumber": "GE Ports",
    "fenumber": "FE Ports",
    "potsnumber": "POTS Ports",
    "wifi": "WiFi",
    "catv": "CATV",
    "ctcautoneg": "CTC Auto Negotiation",
    "ctcversion": "CTC Version",
    "firstuptime": "First UpTime",
    "lastuptime": "Last UpTime",
    "lastofftime": "Last OffTime",
    "onlinetime": "Current Online Time",
    "offlineeventcnt": "Offline Case Count",
}
_WRITE_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def client_from_row(olt) -> SnmpClient:
    return SnmpClient(olt.ip, dec(olt.snmp_community_enc), olt.snmp_port, timeout=5, retries=2)


def _value(values: dict[str, str], oid: str) -> str:
    return next((v for k, v in values.items() if k.endswith(oid)), "")


def _onu_index(index: str) -> tuple[int, int] | None:
    match = INDEX_RE.search(index)
    if not match:
        return None
    pon, number = int(match.group(1)), int(match.group(2))
    if not 1 <= pon <= 8 or number < 1:
        return None
    return pon, number


async def probe(host: str, community: str, port: int) -> dict:
    result = {"snmp_ok": False, "snmp_msg": "", "telnet_ok": False,
              "telnet_msg": "Telnet is not used for read-only HiOSO integration.",
              "sys_descr": "", "uptime_s": None, "sys_object_id": ""}
    client = SnmpClient(host, community, port, timeout=5, retries=1)
    try:
        values = await client.get(SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, FIRMWARE)
        object_id = _value(values, SYS_OBJECT_ID).lstrip(".")
        firmware = _value(values, FIRMWARE)
        result.update(
            sys_descr=_value(values, SYS_DESCR)[:300],
            sys_object_id=object_id,
            uptime_s=ticks_to_seconds(_value(values, SYS_UPTIME)),
        )
        if not object_id.startswith("1.3.6.1.4.1.25355") or "HA7308C" not in firmware.upper():
            result["snmp_msg"] = "SNMP responded, but the device did not identify as a HIOSO HA7308C."
        else:
            result["snmp_ok"], result["snmp_msg"] = True, "SNMP OK — HIOSO HA7308C detected"
    except SnmpError as exc:
        result["snmp_msg"] = str(exc)
    return result


async def test_telnet(host: str, port: int, username: str, password: str, enable_password: str = "") -> str:
    client = TelnetSession(host, port, connect_timeout=15)
    try:
        await client.login(username, password, enable_password)
        return "Telnet login OK — HiOSO CLI enable prompt reached."
    finally:
        await client.close()


async def run_cli(olt, commands: list[str]) -> list[str]:
    username = olt.telnet_user.strip()
    try:
        password = dec(olt.telnet_pass_enc)
        enable_password = dec(olt.enable_pass_enc)
    except ValueError as exc:
        raise TelnetError("Could not decrypt the HIOSO Telnet credentials.") from exc
    if not username or not password:
        raise TelnetError("Configure HIOSO Telnet credentials in OLT settings before using CLI features.")

    async with _WRITE_LOCKS[olt.id]:
        client = TelnetSession(olt.ip, olt.telnet_port, connect_timeout=15)
        try:
            await client.login(username, password, enable_password)
            output = []
            for command in commands:
                result = await client.run(command, overall=20, idle=10)
                if not result.complete:
                    raise TelnetError(f"HIOSO CLI timed out while running '{command}'.")
                if result.error:
                    raise TelnetError(
                        f"HIOSO rejected '{command}': {result.text.strip()[:240] or 'empty response'}"
                    )
                output.append(result.text)
            return output
        finally:
            await client.close()


def sanitize_onu_name(value: str) -> str:
    """Apply the documented HA7304 CLI label constraints before building a command."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip()).strip("_.-")[:32].strip("_.-")
    if not name:
        raise ValueError("ONU name must contain at least one letter or number.")
    return name


def rename_commands(pon: int, number: int, name: str) -> tuple[str, list[str]]:
    if not 1 <= pon <= 8 or not 1 <= number <= 64:
        raise ValueError("Invalid HIOSO EPON port or ONU number.")
    safe_name = sanitize_onu_name(name)
    commands = ["conf t", f"interface epon 0/{pon}", f"onu {number} name {safe_name}", "end"]
    return safe_name, commands


def reboot_commands(pon: int, number: int) -> list[str]:
    if not 1 <= pon <= 8 or not 1 <= number <= 64:
        raise ValueError("Invalid HIOSO EPON port or ONU number.")
    return ["conf t", f"interface epon 0/{pon}", f"onu {number} reboot", "end"]


async def system_info(client: SnmpClient) -> dict:
    values = await client.get(SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, FIRMWARE, FW_VERSION)
    firmware = _value(values, FIRMWARE)
    version = _value(values, FW_VERSION)
    if not _value(values, SYS_OBJECT_ID).lstrip(".").startswith("1.3.6.1.4.1.25355") or "HA7308C" not in firmware.upper():
        raise SnmpError("The SNMP device did not identify as a HIOSO HA7308C.")
    return {
        "sys_descr": _value(values, SYS_DESCR)[:300],
        "sys_object_id": _value(values, SYS_OBJECT_ID),
        "uptime_s": ticks_to_seconds(_value(values, SYS_UPTIME)),
        "firmware": firmware,
        "version": version,
    }


async def read_onus(client: SnmpClient) -> dict[tuple[int, int], dict]:
    tables = await asyncio.gather(
        client.walk(ONU_NAME, max_rep=25, total=90),
        client.walk(ONU_MAC, max_rep=25, total=90),
        client.walk(ONU_RX, max_rep=25, total=90),
        client.walk(ONU_STATE, max_rep=25, total=90),
    )
    onus: dict[tuple[int, int], dict] = {}
    for field, table in zip(("name", "mac", "rx", "state"), tables):
        for suffix, value in table.items():
            index = _onu_index(suffix)
            if index is None:
                continue
            onus.setdefault(index, {})[field] = value.strip().strip('"')
    return {
        key: item for key, item in onus.items()
        if re.fullmatch(r"[0-9a-fA-F]{12}", item.get("mac", ""))
    }


async def read_onu_macs(olt, onus: dict[tuple[int, int], dict]) -> dict[tuple[int, int], list[str]]:
    output = (await run_cli(olt, ["show mac dynamic"]))[0]
    learned: dict[tuple[int, int], set[str]] = defaultdict(set)
    for line in output.splitlines():
        match = MAC_TABLE_ROW_RE.match(line)
        if not match:
            continue
        position = onu_position_from_ifname(match.group(2))
        if position in onus:
            learned[position].add(match.group(1).upper())
    return {position: sorted(macs) for position, macs in learned.items()}


def parse_onu_detail(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        match = re.match(r"\s*([^:]+?)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        key = re.sub(r"[\s_-]+", "", match.group(1)).casefold()
        label = DETAIL_FIELDS.get(key)
        if label and match.group(2):
            fields[label] = match.group(2)
    return fields


async def read_onu_detail(olt, pon: int, number: int) -> dict[str, str]:
    if not 1 <= pon <= 8 or not 1 <= number <= 64:
        raise ValueError("Invalid HIOSO EPON port or ONU number.")
    outputs = await run_cli(olt, [f"show onu info epon 0/{pon} {number}"])
    fields = parse_onu_detail(outputs[0])
    if not fields:
        raise TelnetError("HIOSO returned no recognizable ONU detail fields.")
    return fields


async def read_interfaces(client: SnmpClient) -> dict[str, dict]:
    names, incoming, outgoing = await asyncio.gather(
        client.walk(IF_NAME, max_rep=25, total=30),
        client.walk(IF_IN, max_rep=25, total=30),
        client.walk(IF_OUT, max_rep=25, total=30),
    )
    rows = {}
    for index, name in names.items():
        if not name:
            continue
        inc, out = incoming.get(index), outgoing.get(index)
        if inc is None or out is None:
            continue
        try:
            rows[name] = (int(index), int(inc), int(out))
        except ValueError:
            continue
    return rows
