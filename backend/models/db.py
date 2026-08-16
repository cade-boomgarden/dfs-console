from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..settings import get_settings


class Base(DeclarativeBase):
    pass


def _engine():
    s = get_settings()
    kw = {}
    if s.database_url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    return create_engine(s.database_url, **kw)


engine = _engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
