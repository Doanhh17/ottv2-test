"""Khởi tạo engine và session SQLAlchemy cho SQLite hoặc PostgreSQL."""

from collections.abc import Generator
import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

import config
from models import Base


def create_database_engine(database_url: str = config.DATABASE_URL) -> Engine:
    """Tạo engine với tùy chọn kết nối phù hợp môi trường."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    if database_url.startswith("sqlite"):
        database_path = make_url(database_url).database
        if database_path and database_path != ":memory:":
            parent = Path(database_path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(parent, 0o700)
    return create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )


engine = create_database_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_database(target_engine: Engine = engine) -> None:
    """Tạo các bảng ORM nếu chưa tồn tại."""
    Base.metadata.create_all(target_engine)


def get_session() -> Generator[Session, None, None]:
    """Cấp session và đảm bảo luôn đóng kết nối sau khi dùng."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
