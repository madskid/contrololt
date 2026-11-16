"""Async client for the native MikroTik RouterOS API (TCP 8728 / TLS 8729)."""
import asyncio
import hashlib
import ssl
from typing import Any

_DEFAULT_TIMEOUT = 10.0
_SECRET_PROPS = ".id,name,profile,service,comment,disabled,caller-id"
_SECRET_CREDENTIAL_PROPS = f"{_SECRET_PROPS},password"
_ACTIVE_PROPS = ".id,name,service,caller-id,address,uptime,encoding,session-id,radius"
_PROFILE_PROPS = "name,rate-limit,local-address,remote-address"


class MikroError(Exception):
    """Raised when the router returns an error or is unreachable."""


def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes((length,))
    if length < 0x4000:
        return bytes(((length >> 8) | 0x80, length & 0xFF))
    if length < 0x200000:
        return bytes(((length >> 16) | 0xC0, (length >> 8) & 0xFF, length & 0xFF))
    if length < 0x10000000:
        return bytes(((length >> 24) | 0xE0, (length >> 16) & 0xFF,
                      (length >> 8) & 0xFF, length & 0xFF))
    if length <= 0xFFFFFFFF:
        return b"\xF0" + length.to_bytes(4, "big")
    raise ValueError("RouterOS API word is too large")


async def _read_word(reader: asyncio.StreamReader) -> bytes:
    first = (await reader.readexactly(1))[0]
    if first < 0x80:
        length = first
    elif first < 0xC0:
        length = ((first & 0x3F) << 8) | (await reader.readexactly(1))[0]
    elif first < 0xE0:
        tail = await reader.readexactly(2)
        length = ((first & 0x1F) << 16) | int.from_bytes(tail, "big")
    elif first < 0xF0:
        tail = await reader.readexactly(3)
        length = ((first & 0x0F) << 24) | int.from_bytes(tail, "big")
    elif first == 0xF0:
        length = int.from_bytes(await reader.readexactly(4), "big")
    else:
        raise MikroError("Invalid word length received from RouterOS API")
    return await reader.readexactly(length)


def _encode_sentence(words: list[str]) -> bytes:
    parts = []
    for word in words:
        raw = word.encode("utf-8")
        parts.append(_encode_length(len(raw)))
        parts.append(raw)
    parts.append(b"\x00")
    return b"".join(parts)


def _parse_sentence(words: list[str]) -> tuple[str, dict[str, str]]:
    kind = words[0] if words else ""
    attrs: dict[str, str] = {}
    for word in words[1:]:
        if word.startswith("=") and "=" in word[1:]:
            key, value = word[1:].split("=", 1)
            attrs[key] = value
    return kind, attrs


class MikroClient:
    """Small async RouterOS API client; it opens a fresh native API session per command."""

    def __init__(self, ip: str, user: str, password: str, port: int = 8728,
                 use_ssl: bool = False, timeout: float = _DEFAULT_TIMEOUT):
        self._ip = ip
        self._user = user
        self._password = password
        self._port = port
        self._use_ssl = use_ssl or port == 8729
        self._timeout = timeout

    async def _read_sentence(self, reader: asyncio.StreamReader) -> list[str]:
        words = []
        while True:
            raw = await _read_word(reader)
            if not raw:
                return words
            words.append(raw.decode("utf-8", errors="replace"))

    async def _send_sentence(self, writer: asyncio.StreamWriter, words: list[str]) -> None:
        writer.write(_encode_sentence(words))
        await writer.drain()

    async def _read_reply(self, reader: asyncio.StreamReader) -> tuple[list[dict[str, str]], dict[str, str]]:
        rows = []
        done: dict[str, str] = {}
        while True:
            kind, attrs = _parse_sentence(await self._read_sentence(reader))
            if kind == "!re":
                rows.append(attrs)
            elif kind == "!done":
                done = attrs
                return rows, done
            elif kind in ("!trap", "!fatal"):
                message = attrs.get("message", "RouterOS API command failed")
                category = attrs.get("category", "")
                suffix = f" (category {category})" if category else ""
                raise MikroError(message + suffix)
            else:
                raise MikroError(f"Unexpected RouterOS API reply: {kind or '(empty)'}")

    async def _login(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._send_sentence(writer, [
            "/login", f"=name={self._user}", f"=password={self._password}",
        ])
        try:
            _, done = await self._read_reply(reader)
            if "ret" not in done:
                return
            challenge = bytes.fromhex(done["ret"])
        except MikroError:
            # RouterOS versions before 6.43 use the challenge/response handshake.
            await self._send_sentence(writer, ["/login"])
            _, reply = await self._read_reply(reader)
            challenge_hex = reply.get("ret")
            if not challenge_hex:
                raise
            challenge = bytes.fromhex(challenge_hex)

        digest = hashlib.md5(b"\x00" + self._password.encode("utf-8") + challenge).hexdigest()
        await self._send_sentence(writer, [
            "/login", f"=name={self._user}", f"=response=00{digest}",
        ])
        await self._read_reply(reader)

    async def _run(self, words: list[str]) -> tuple[list[dict[str, str]], dict[str, str]]:
        context = None
        if self._use_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        reader = writer = None
        try:
            reader, writer = await asyncio.open_connection(
                self._ip, self._port, ssl=context,
                server_hostname=self._ip if context else None,
            )
            await self._login(reader, writer)
            await self._send_sentence(writer, words)
            return await self._read_reply(reader)
        except (OSError, asyncio.IncompleteReadError, ssl.SSLError) as exc:
            raise MikroError(f"Connection error to {self._ip}:{self._port}: {exc}") from exc
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def _command(self, *words: str) -> tuple[list[dict[str, str]], dict[str, str]]:
        try:
            return await asyncio.wait_for(self._run(list(words)), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise MikroError(f"Timeout connecting to {self._ip}:{self._port}") from exc

    async def _print(self, path: str, proplist: str, *query: str) -> list[dict[str, str]]:
        rows, _ = await self._command(f"{path}/print", f"=.proplist={proplist}", *query)
        return rows

    async def _find_secret(self, secret_id: str) -> dict[str, str] | None:
        secrets = await self._print("/ppp/secret", _SECRET_PROPS, "?service=pppoe")
        return next((secret for secret in secrets if secret.get(".id") == secret_id), None)

    async def system_identity(self) -> dict[str, str]:
        rows = await self._print("/system/identity", "name")
        return rows[0] if rows else {}

    async def system_resource(self) -> dict[str, str]:
        rows = await self._print("/system/resource", "version,board-name,uptime")
        return rows[0] if rows else {}

    async def ping_test(self) -> dict[str, str]:
        identity, resource = await asyncio.gather(self.system_identity(), self.system_resource())
        return {"identity": identity.get("name", ""), **resource}

    async def pppoe_profiles(self) -> list[dict[str, str]]:
        return await self._print("/ppp/profile", _PROFILE_PROPS)

    async def pppoe_secrets(self, query: str = "") -> list[dict[str, str]]:
        secrets = await self._print("/ppp/secret", _SECRET_PROPS, "?service=pppoe")
        if query:
            lowered = query.casefold()
            secrets = [secret for secret in secrets
                       if lowered in secret.get("name", "").casefold()]
        return secrets

    async def get_secret(self, secret_id: str) -> dict[str, str]:
        secret = await self._find_secret(secret_id)
        if secret is None:
            raise MikroError("PPPoE user not found")
        return secret

    async def get_secret_credentials(self, secret_id: str) -> dict[str, str]:
        secrets = await self._print(
            "/ppp/secret", _SECRET_CREDENTIAL_PROPS, "?service=pppoe",
        )
        secret = next((item for item in secrets if item.get(".id") == secret_id), None)
        if secret is None:
            raise MikroError("PPPoE user not found")
        if not secret.get("password"):
            raise MikroError("RouterOS did not return a password for this PPPoE user")
        return secret

    async def add_secret(self, name: str, password: str, profile: str,
                         comment: str = "", service: str = "pppoe") -> dict[str, str]:
        await self._command(
            "/ppp/secret/add", f"=name={name}", f"=password={password}",
            f"=profile={profile}", f"=service={service}", f"=comment={comment}",
        )
        matches = await self.pppoe_secrets(name)
        secret = next((item for item in matches if item.get("name") == name), None)
        return secret or {"name": name, "profile": profile, "comment": comment, "service": service}

    async def edit_secret(self, secret_id: str, **fields: Any) -> dict[str, str]:
        attrs = ["/ppp/secret/set", f"=.id={secret_id}"]
        attrs.extend(f"={key}={value}" for key, value in fields.items() if value is not None)
        await self._command(*attrs)
        return await self.get_secret(secret_id)

    async def delete_secret(self, secret_id: str) -> None:
        await self._command("/ppp/secret/remove", f"=.id={secret_id}")

    async def copy_secret(self, secret_id: str, new_name: str, new_password: str) -> dict[str, str]:
        original = await self.get_secret(secret_id)
        attrs = {
            "name": new_name,
            "password": new_password,
            "profile": original.get("profile", ""),
            "service": original.get("service", "pppoe"),
            "comment": original.get("comment", ""),
        }
        for key in ("caller-id", "disabled"):
            if key in original:
                attrs[key] = original[key]
        await self._command("/ppp/secret/add", *(f"={key}={value}" for key, value in attrs.items()))
        return await self.get_secret_by_name(new_name)

    async def get_secret_by_name(self, name: str) -> dict[str, str]:
        matches = await self.pppoe_secrets(name)
        secret = next((item for item in matches if item.get("name") == name), None)
        if secret is None:
            raise MikroError("New PPPoE user was not found after creation")
        return secret

    async def pppoe_active(self, query: str = "") -> list[dict[str, str]]:
        sessions = await self._print("/ppp/active", _ACTIVE_PROPS, "?service=pppoe")
        if query:
            lowered = query.casefold()
            sessions = [session for session in sessions
                        if lowered in session.get("name", "").casefold()]
        return sessions

    async def disconnect_active(self, active_id: str) -> None:
        await self._command("/ppp/active/remove", f"=.id={active_id}")

    async def active_caller_id(self, username: str) -> str | None:
        sessions = await self._print("/ppp/active", _ACTIVE_PROPS, f"?name={username}")
        if sessions:
            return sessions[0].get("caller-id")
        return None
