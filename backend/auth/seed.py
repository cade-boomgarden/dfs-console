"""Seed a user account: python -m backend.auth.seed <username> [password]"""
from __future__ import annotations

import getpass
import sys

from ..models.db import Base, SessionLocal, engine
from ..models.models import User
from .security import hash_password


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m backend.auth.seed <username> [password]")
        raise SystemExit(1)
    username = sys.argv[1]
    password = sys.argv[2] if len(sys.argv) > 2 else getpass.getpass("password: ")

    Base.metadata.create_all(engine)
    db = SessionLocal()
    existing = db.query(User).filter_by(username=username).first()
    if existing:
        existing.password_hash = hash_password(password)
        print(f"updated password for {username}")
    else:
        db.add(User(username=username, password_hash=hash_password(password)))
        print(f"created {username}")
    db.commit()


if __name__ == "__main__":
    main()
