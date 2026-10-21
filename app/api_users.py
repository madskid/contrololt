"""Administrator-only user management."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security import hash_password, require_admin, validate_new_password, validate_username

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_admin)])
Role = Literal["administrator", "read_only"]


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=60)
    password: str = Field(min_length=5, max_length=256)
    role: Role = "read_only"


class UserUpdate(BaseModel):
    role: Role
    password: Optional[str] = Field(None, min_length=5, max_length=256)


def user_dict(user: User) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role,
            "created_at": user.created_at.isoformat() if user.created_at else None}


@router.get("")
async def list_users(s: AsyncSession = Depends(get_session)):
    users = (await s.scalars(select(User).order_by(User.username))).all()
    return [user_dict(user) for user in users]


@router.post("", status_code=201)
async def create_user(body: UserCreate, s: AsyncSession = Depends(get_session)):
    username = validate_username(body.username)
    if (await s.scalars(select(User.id).where(User.username == username))).first():
        raise HTTPException(409, "A user with this username already exists.")
    user = User(username=username, password_hash=hash_password(validate_new_password(body.password)), role=body.role)
    s.add(user)
    await s.commit()
    await s.refresh(user)
    return user_dict(user)


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
    user.role = body.role
    if body.password:
        user.password_hash = hash_password(validate_new_password(body.password))
    await s.commit()
    await s.refresh(user)
    return user_dict(user)


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
