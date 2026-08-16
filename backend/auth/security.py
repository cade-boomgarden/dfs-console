"""Auth (section 13, DECIDED): signed session cookie + argon2, ~80 lines.

No self-signup, no password reset. Accounts are seeded by script
(`python -m backend.auth.seed <username>`).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from ..models.db import get_db
from ..models.models import User

pwd = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(p: str) -> str:
    return pwd.hash(p)


def verify_password(p: str, h: str) -> bool:
    try:
        return pwd.verify(p, h)
    except Exception:
        return False


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="Not signed in")
    user = db.get(User, uid)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not signed in")
    return user
