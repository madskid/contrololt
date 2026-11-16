"""Administrator-only user management."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import OLT, User, UserOltAccess
from app.security import hash_password, require_admin, validate_new_password, validate_username

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_admin)])
Role = Literal["administrator", "read_only", "olt_limited"]


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=60)
    password: str = Field(min_length=5, max_length=256)
    role: Role = "read_only"
    olt_ids: list[int] = Field(default_factory=list, max_length=500)


class UserUpdate(BaseModel):
    role: Role
    password: Optional[str] = Field(None, min_length=5, max_length=256)
    olt_ids: list[int] = Field(default_factory=list, max_length=500)


def user_dict(user: User, olt_ids: list[int] | None = None) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role,
            "olt_ids": olt_ids or [],
            "created_at": user.created_at.isoformat() if user.created_at else None}


async def validate_olt_ids(s: AsyncSession, role: str, olt_ids: list[int]) -> list[int]:
    selected = sorted(set(olt_ids)) if role == "olt_limited" else []
    if role == "olt_limited" and not selected:
        raise HTTPException(422, "Select at least one OLT for an OLT-limited user.")
    if selected:
        found = set(await s.scalars(select(OLT.id).where(OLT.id.in_(selected))))
        if found != set(selected):
            raise HTTPException(422, "One or more selected OLTs do not exist.")
    return selected


async def replace_olt_access(s: AsyncSession, user_id: int, olt_ids: list[int]) -> None:
    await s.execute(delete(UserOltAccess).where(UserOltAccess.user_id == user_id))
    s.add_all(UserOltAccess(user_id=user_id, olt_id=olt_id) for olt_id in olt_ids)


@router.get("")
async def list_users(s: AsyncSession = Depends(get_session)):
    users = (await s.scalars(select(User).order_by(User.username))).all()
    assignments = (await s.execute(select(UserOltAccess.user_id, UserOltAccess.olt_id))).all()
    access = {}
    for user_id, olt_id in assignments:
        access.setdefault(user_id, []).append(olt_id)
    return [user_dict(user, access.get(user.id, [])) for user in users]


@router.get("/olt-options")
async def olt_options(s: AsyncSession = Depends(get_session)):
    return [{"id": olt.id, "name": olt.name} for olt in
            (await s.scalars(select(OLT).order_by(OLT.name))).all()]


@router.post("", status_code=201)
async def create_user(body: UserCreate, s: AsyncSession = Depends(get_session)):
    username = validate_username(body.username)
    if (await s.scalars(select(User.id).where(User.username == username))).first():
        raise HTTPException(409, "A user with this username already exists.")
    olt_ids = await validate_olt_ids(s, body.role, body.olt_ids)
    user = User(username=username, password_hash=hash_password(validate_new_password(body.password)), role=body.role)
    s.add(user)
    await s.flush()
    await replace_olt_access(s, user.id, olt_ids)
    await s.commit()
    await s.refresh(user)
    return user_dict(user, olt_ids)


@router.put("/{user_id}")
async def update_user(user_id: int, body: UserUpdate, admin: str = Depends(require_admin),
                      s: AsyncSession = Depends(get_session)):
    user = await s.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    if user.username == admin and body.role != "administrator":
        raise HTTPException(409, "You cannot remove your own administrator role.")
    if user.role == "administrator" and body.role != "administrator":
        admins = await s.scalar(select(func.count()).select_from(User).where(User.role == "administrator"))
        if admins <= 1:
            raise HTTPException(409, "At least one administrator account must remain.")
    olt_ids = await validate_olt_ids(s, body.role, body.olt_ids)
    user.role = body.role
    if body.password:
        user.password_hash = hash_password(validate_new_password(body.password))
    await replace_olt_access(s, user.id, olt_ids)
    await s.commit()
    await s.refresh(user)
    return user_dict(user, olt_ids)


@router.delete("/{user_id}")
async def delete_user(user_id: int, admin: str = Depends(require_admin), s: AsyncSession = Depends(get_session)):
    user = await s.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    if user.username == admin:
        raise HTTPException(409, "You cannot delete your own account.")
    if user.role == "administrator":
        admins = await s.scalar(select(func.count()).select_from(User).where(User.role == "administrator"))
        if admins <= 1:
            raise HTTPException(409, "At least one administrator account must remain.")
    await s.delete(user)
    await s.commit()
    return {"ok": True}
