"""SNMP v2c via the net-snmp CLI (snmpget / snmpbulkwalk).

The CLI is stable across Python versions, and one snmpbulkwalk retrieves an entire table
(status/signal for thousands of ONUs) in one process instead of one process per OID per ONU.
"""
import asyncio
import re
import shutil

from app.config import settings

_SEM: asyncio.Semaphore | None = None

QBRIDGE_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
BRIDGE_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
BRIDGE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(settings.snmp_concurrency)
    return _SEM


class SnmpError(Exception):
    pass


def parse_walk(text: str, base: str | None = None) -> dict[str, str]:
    """Output `-On -Oq` ('.1.3.6... nilai') -> {suffix_atau_oid: nilai}."""
    out: dict[str, str] = {}
    prefix = "." + base.strip(".") + "." if base else None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(".") or " " not in line:
            continue
        oid, value = line.split(None, 1)
        if prefix:
            if not oid.startswith(prefix):
                continue
            oid = oid[len(prefix):]
        out[oid] = value.strip().strip('"')
    return out


class SnmpClient:
    def __init__(self, host: str, community: str, port: int = 161,
                 timeout: int | None = None, retries: int = 1):
        self.host, self.community, self.port = host, community, port
        self.timeout = timeout or settings.snmp_timeout
        self.retries = retries

    async def _run(self, tool: str, *args: str, total: float) -> str:
        if not shutil.which(tool):
            raise SnmpError(f"{tool} not found (install the 'snmp' package)")
        cmd = [tool, "-v2c", "-c", self.community, "-t", str(self.timeout), "-r", str(self.retries),
               "-On", "-Oq", "-Oe", "-Ot", *args]
        async with _sem():
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), total)
            except asyncio.TimeoutError:
                proc.kill()
                raise SnmpError(f"SNMP timeout connecting to {self.host}:{self.port}")
        text = out.decode("utf-8", "ignore")
        if proc.returncode != 0:
            msg = (err.decode("utf-8", "ignore") or text).strip().splitlines()
            raise SnmpError(msg[-1] if msg else f"{tool} failed (exit code {proc.returncode})")
        return text

    async def get(self, *oids: str) -> dict[str, str]:
        text = await self._run("snmpget", f"{self.host}:{self.port}", *oids,
                               total=self.timeout * (self.retries + 1) + 3)
        return parse_walk(text)

    async def walk(self, oid: str, max_rep: int = 50, total: float = 120) -> dict[str, str]:
        text = await self._run("snmpbulkwalk", f"-Cr{max_rep}", f"{self.host}:{self.port}", oid, total=total)
        if "No Such" in text or "No more variables" in text:
            text = "\n".join(l for l in text.splitlines() if l.startswith("."))
        return parse_walk(text, oid)


def _integer(value: str) -> int | None:
    match = re.search(r"(-?\d+)\s*$", value or "")
    return int(match.group(1)) if match else None


def _mac_from_fdb_suffix(suffix: str) -> str:
    try:
        octets = [int(part) for part in suffix.split(".")]
    except ValueError:
        return ""
    if len(octets) < 6 or any(not 0 <= octet <= 255 for octet in octets[-6:]):
        return ""
    return ":".join(f"{octet:02X}" for octet in octets[-6:])


async def read_bridge_mac_table(client: SnmpClient) -> list[tuple[str, str]]:
    """Return learned (interface name, MAC) rows using standard Q-BRIDGE/BRIDGE MIBs."""
    fdb_rows: list[tuple[int, str]] | None = None
    walk_errors = []
    for oid in (QBRIDGE_FDB_PORT, BRIDGE_FDB_PORT):
        try:
            table = await client.walk(oid, max_rep=25, total=90)
        except SnmpError as exc:
            walk_errors.append(str(exc))
            continue
        rows = []
        for suffix, value in table.items():
            port = _integer(value)
            mac = _mac_from_fdb_suffix(suffix)
            if port is not None and port > 0 and mac:
                rows.append((port, mac))
        if rows:
            fdb_rows = rows
            break
        if fdb_rows is None:
            fdb_rows = []
    if fdb_rows is None:
        raise SnmpError("Could not read the OLT bridge MAC table: " + "; ".join(walk_errors))
    if not fdb_rows:
        return []

    try:
        port_indexes = await client.walk(BRIDGE_PORT_IFINDEX, max_rep=25, total=30)
    except SnmpError as exc:
        raise SnmpError(f"Could not map bridge ports to interface indexes: {exc}") from exc
    if not port_indexes:
        raise SnmpError("The OLT returned no bridge-port to interface-index mappings.")

    interfaces: dict[int, str] = {}
    interface_errors = []
    for oid in (IF_NAME, IF_DESCR):
        try:
            names = await client.walk(oid, max_rep=25, total=30)
        except SnmpError as exc:
            interface_errors.append(str(exc))
            continue
        for suffix, value in names.items():
            index = _integer(suffix)
            name = re.sub(r"^(?:STRING|OCTET STRING):\s*", "", value, flags=re.IGNORECASE).strip().strip('"')
            if index is not None and name:
                interfaces[index] = name
        if interfaces:
            break
    if not interfaces:
        details = "; ".join(interface_errors)
        raise SnmpError("Could not map learned MACs to OLT interfaces" + (f": {details}" if details else "."))

    mapped = set()
    for bridge_port, mac in fdb_rows:
        if_index = _integer(port_indexes.get(str(bridge_port), ""))
        if if_index is None:
            continue
        name = interfaces.get(if_index)
        if name:
            mapped.add((name, mac))
    return sorted(mapped)


def onu_position_from_ifname(name: str) -> tuple[int, int] | None:
    """Parse logical EPON labels such as EPON0/1:23, epon 0/1/23, or epon0/1/23."""
    match = re.search(
        r"\b(?:epon|pon)\s*0?/(\d+)/(\d+)\b",
        name,
        re.IGNORECASE,
    )
    if match:
        pon, number = int(match.group(1)), int(match.group(2))
        if pon > 0 and number > 0:
            return pon, number
    match = re.search(
        r"\b(?:epon|pon)(?:[-_ ]?olt)?\s*(?:0/)?(\d+)\s*:\s*(\d+)\b",
        name,
        re.IGNORECASE,
    )
    if match:
        pon, number = int(match.group(1)), int(match.group(2))
        if pon > 0 and number > 0:
            return pon, number
    match = re.search(
        r"\b(?:epon|pon)\w*\s*(?:0/)?(\d+).*?\bonu\s*(\d+)\b",
        name,
        re.IGNORECASE,
    )
    if match:
        pon, number = int(match.group(1)), int(match.group(2))
        if pon > 0 and number > 0:
            return pon, number
    return None


def ticks_to_seconds(value: str) -> int | None:
    m = re.search(r"\d+", value or "")
    return int(m.group()) // 100 if m else None
