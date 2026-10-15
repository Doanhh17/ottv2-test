"""Account creation and password hashing helpers."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from models import User


password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return password_hasher.hash(password)


def create_user(
    session: Session,
    student_id: str,
    password: str,
    is_admin: bool = False,
) -> User:
    """Create an account after validating its ID."""
    if re.fullmatch(config.STUDENT_ID_PATTERN, student_id) is None:
        raise ValueError("student_id phải gồm 3–32 ký tự chữ, số, gạch dưới hoặc gạch ngang")
    user = User(
        student_id=student_id,
        password_hash=hash_password(password),
        is_admin=is_admin,
        created_at=datetime.now(ZoneInfo(config.TIMEZONE_NAME)),
    )
    session.add(user)
    session.commit()
    return user


def bootstrap_admin(session: Session, student_id: str, password: str) -> bool:
    """Create the configured bootstrap administrator only when its ID is unused."""
    if session.scalar(select(User.student_id).where(User.student_id == student_id)) is not None:
        return False
    if len(password) < 12:
        raise ValueError("Bootstrap administrator password must be at least 12 characters")
    create_user(session, student_id, password, is_admin=True)
    return True
