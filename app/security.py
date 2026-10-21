"""OLT credential encryption, user authentication, and role dependencies."""
import base64
import hashlib
import hmac
import secrets
import re
import time
from collections import defaultdict

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, get_session
from app.models import User

_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()))


def enc(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode() if value else ""


def dec(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Could not decrypt OLT credentials (has SECRET_KEY changed?)") from exc


PASSWORD_ITERATIONS = 600_000
ROLES = {"administrator", "read_only"}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), int(rounds))
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode(), expected)
    except (ValueError, TypeError):
        return False


async def authenticate_user(username: str, password: str) -> User | None:
    async with SessionLocal() as session:
        user = (await session.scalars(select(User).where(User.username == username))).first()
        if user and verify_password(password, user.password_hash):
            return user
    return None


async def seed_admin(session: AsyncSession) -> None:
    existing = (await session.scalars(select(User.id).limit(1))).first()
    if existing is None:
        session.add(User(username=settings.admin_user, password_hash=hash_password(settings.admin_pass),
                        role="administrator"))
        await session.commit()


def validate_username(username: str) -> str:
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{2,59}", username):
        raise HTTPException(422, "Username must be 3-60 characters and contain only letters, numbers, '.', '_', '@', or '-'.")
    return username


def validate_new_password(password: str) -> str:
    if len(password) < 5:
        raise HTTPException(422, "Password must be at least 5 characters.")
    if len(password) > 256:
        raise HTTPException(422, "Password must be 256 characters or fewer.")
    return password


# Pembatas percobaan login sederhana (per IP, in-memory)
_fails: dict[str, list[float]] = defaultdict(list)
MAX_FAILS, WINDOW = 5, 300


def login_blocked(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails[ip] if now - t < WINDOW]
    return len(_fails[ip]) >= MAX_FAILS


def login_failed(ip: str) -> None:
    _fails[ip].append(time.time())


class NotAuthenticated(Exception):
    pass


async def require_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticated()
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            raise NotAuthenticated()
        request.state.user_role = user.role
        request.state.user_id = user.id
        return user.username


async def require_admin(request: Request, user: str = Depends(require_user)) -> str:
    if request.state.user_role != "administrator":
        raise HTTPException(403, "Administrator access is required.")
    return user
