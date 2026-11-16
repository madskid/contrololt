"""Prompt-driven async Telnet client (not sleep-based), with IAC negotiation and --More-- pagination."""
import asyncio
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("smartolt.telnet")

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA = 1, 3

PROMPT_RE = re.compile(r"(?:^|[\r\n])([\w\-.]+(?:\([\w\-./: ]+\))?[>#])[ ]?$")   # (gpon-onu-mng 1/2/1:64)# berisi spasi
USER_RE = re.compile(r"(?:user ?name|login)\s*:\s*$", re.I)
PASS_RE = re.compile(r"pass(?:word)?\s*:\s*$", re.I)
MORE_RE = re.compile(r"-+\s*more\s*-+", re.I)
ERR_RE = re.compile(r"%\s*error|%\s*code\s*\d+|invalid (?:input|command)|unknown command|incomplete command|"
                    r"ambiguous command|unrecognized command|% invalid|\bfailed\b|already exist", re.I)
CONFIRM_RE = re.compile(r"(?:\[y(?:es)?/n(?:o)?\]|\(y/n\)|are you sure|confirm)[^\n]{0,20}[:?]?\s*$", re.I)
LOGIN_FAIL_RE = re.compile(r"incorrect|fail|invalid|denied|wrong|authentication", re.I)


class TelnetError(Exception):
    pass


class BusyError(Exception):
    """The OLT is in use by another operation (per-OLT lock)."""


@dataclass
class CmdResult:
    text: str
    complete: bool   # prompt akhir terlihat (output utuh)
    error: bool      # OLT menjawab error/invalid command


def strip_iac(buf: bytes) -> tuple[bytes, bytes, bytes]:
    """Remove IAC sequences from a buffer. Return (clean data, negotiation reply, incomplete remainder)."""
    out, reply, i, n = bytearray(), bytearray(), 0, len(buf)
    while i < n:
        b = buf[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            return bytes(out), bytes(reply), buf[i:]
        c = buf[i + 1]
        if c == IAC:
            out.append(IAC)
            i += 2
        elif c in (DO, DONT, WILL, WONT):
            if i + 2 >= n:
                return bytes(out), bytes(reply), buf[i:]
            opt = buf[i + 2]
            if c == DO:
                reply += bytes([IAC, WONT, opt])
            elif c == WILL:
                reply += bytes([IAC, DO if opt in (OPT_ECHO, OPT_SGA) else DONT, opt])
            i += 3
        elif c == SB:
            j = buf.find(bytes([IAC, SE]), i + 2)
            if j < 0:
                return bytes(out), bytes(reply), buf[i:]
            i = j + 2
        else:
            i += 2
    return bytes(out), bytes(reply), b""


def clean_output(text: str, cmd: str) -> str:
    text = MORE_RE.sub("", text)
    text = re.sub(r"(?:\x08 \x08)+|\x08+", "", text).replace("\r", "")
    lines = text.split("\n")
    if lines and cmd and lines[0].strip().endswith(cmd.strip()):
        lines = lines[1:]
    if lines and PROMPT_RE.search("\n" + lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


class TelnetSession:
    def __init__(self, host: str, port: int = 23, connect_timeout: float = 10):
        self.host, self.port, self.connect_timeout = host, port, connect_timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pending = b""

    @property
    def closed(self) -> bool:
        return self.writer is None or self.writer.is_closing()

    async def _read(self, timeout: float) -> str:
        data = await asyncio.wait_for(self.reader.read(65536), timeout)
        if not data:
            raise TelnetError("Connection closed by the OLT")
        clean, reply, self._pending = strip_iac(self._pending + data)
        if reply:
            self.writer.write(reply)
            await self.writer.drain()
        return clean.decode("utf-8", "ignore")

    async def _collect(self, end_res, overall: float, idle: float, paginate: bool = False):
        """Baca sampai salah satu regex di end_res cocok di ujung buffer. Return (teks, index|-1)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + overall
        chunks: list[str] = []
        window = ""
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "".join(chunks), -1
            try:
                txt = await self._read(min(idle, remaining))
            except asyncio.TimeoutError:
                return "".join(chunks), -1
            chunks.append(txt)
            window = (window + txt)[-300:]
            if paginate and MORE_RE.search(window):
                self.writer.write(b" ")
                await self.writer.drain()
                window = MORE_RE.sub("", window)
                continue
            for idx, rx in enumerate(end_res):
                if rx.search(window):
                    # beri kesempatan 50ms: pastikan benar-benar prompt akhir, bukan potongan teks
                    try:
                        more = await self._read(0.05)
                        chunks.append(more)
                        window = (window + more)[-300:]
                        if rx.search(window):
                            return "".join(chunks), idx
                        continue
                    except (asyncio.TimeoutError, TelnetError):  # No more data or the OLT closed the connection.
                        return "".join(chunks), idx

    async def login(self, user: str, password: str, enable_password: str = "") -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.connect_timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise TelnetError(f"Could not connect to {self.host}:{self.port} ({exc or 'timeout'})") from exc

        text, idx = await self._collect([USER_RE, PASS_RE, PROMPT_RE], self.connect_timeout, self.connect_timeout)
        if idx == -1:
            raise TelnetError("No login prompt received from the OLT")
        if idx == 0:
            await self._send(user)
            text, idx = await self._collect([PASS_RE, PROMPT_RE], 8, 8)
        if idx in (0, 1) and PASS_RE.search(text[-80:]):
            await self._send(password)
            try:
                text, idx = await self._collect([PROMPT_RE, USER_RE], 10, 10)
            except TelnetError:
                raise TelnetError("Login rejected (OLT closed the connection — incorrect username/password?)")
            if idx != 0:
                raise TelnetError("Login rejected (incorrect username/password?)")
        elif idx == -1:
            raise TelnetError("No password prompt received from the OLT")

        prompt = PROMPT_RE.search(text[-120:])
        if prompt and prompt.group(1).endswith(">"):
            await self._send("enable")
            text, idx = await self._collect([PASS_RE, PROMPT_RE], 8, 8)
            if idx == 0:
                await self._send(enable_password or password)
                text, idx = await self._collect([PROMPT_RE], 8, 8)
            if idx == -1 or not (PROMPT_RE.search(text[-120:]) or PROMPT_RE.search("\n" + text)).group(1).endswith("#"):
                raise TelnetError("Failed to enter enable mode (incorrect enable password?)")
        await self.run("terminal length 0", overall=8, idle=5)

    async def _send(self, line: str) -> None:
        self.writer.write((line + "\r\n").encode())
        await self.writer.drain()

    async def run(self, cmd: str, overall: float = 60, idle: float = 20, confirm: bool = False) -> CmdResult:
        """Send a command and wait for the prompt. With confirm=True, answer 'yes' to [yes/no] prompts."""
        if self.closed:
            raise TelnetError("Telnet session is already closed")
        await self._send(cmd)
        parts: list[str] = []
        ends = [PROMPT_RE] + ([CONFIRM_RE] if confirm else [])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + overall
        idx = -1
        for _ in range(4):                       # At most three confirmations per command.
            raw, idx = await self._collect(ends, max(1.0, deadline - loop.time()), idle, paginate=True)
            parts.append(raw)
            if idx == 1:
                await self._send("yes")
                continue
            break
        text = clean_output("".join(parts), cmd)
        return CmdResult(text=text, complete=idx == 0, error=bool(ERR_RE.search(text[:400])))

    async def close(self) -> None:
        if self.writer:
            try:
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), 2)
            except Exception:
                pass
        self.reader = self.writer = None
