"""SNMP v2c via the net-snmp CLI (snmpget / snmpbulkwalk).

The CLI is stable across Python versions, and one snmpbulkwalk retrieves an entire table
(status/signal for thousands of ONUs) in one process instead of one process per OID per ONU.
"""
import asyncio
import re
import shutil

from app.config import settings

_SEM: asyncio.Semaphore | None = None


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


def ticks_to_seconds(value: str) -> int | None:
    m = re.search(r"\d+", value or "")
    return int(m.group()) // 100 if m else None
