"""Tạo một admin và năm tài khoản thí sinh thử nghiệm trên SQLite local."""

import argparse
import os
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from sqlalchemy.orm import Session

import config
from db import create_database_engine, initialize_database
from models import User


DEMO_ACCOUNTS = (
    ("admin_demo", True),
    ("bot_demo01", False),
    ("bot_demo02", False),
    ("bot_demo03", False),
    ("bot_demo04", False),
    ("bot_demo05", False),
)


def seed_demo_accounts(
    database_url: str = config.DATABASE_URL,
    credential_path: str | Path = config.STORAGE_DIR / "demo-accounts.txt",
) -> tuple[tuple[str, str, bool], ...]:
    """Tạo hoặc xoay vòng mật khẩu sáu tài khoản demo, chỉ trên SQLite."""
    if not database_url.startswith("sqlite:"):
        raise ValueError("Tài khoản demo chỉ được tạo trên SQLite local")
    database_engine = create_database_engine(database_url)
    initialize_database(database_engine)
    hasher = PasswordHasher()
    now = datetime.now(ZoneInfo(config.TIMEZONE_NAME))
    accounts: list[tuple[str, str, bool]] = []
    try:
        with Session(database_engine) as session:
            for student_id, is_admin in DEMO_ACCOUNTS:
                password = secrets.token_urlsafe(18)
                user = session.get(User, student_id)
                if user is None:
                    user = User(
                        student_id=student_id,
                        password_hash=hasher.hash(password),
                        is_admin=is_admin,
                        created_at=now,
                    )
                    session.add(user)
                else:
                    user.password_hash = hasher.hash(password)
                    user.is_admin = is_admin
                accounts.append((student_id, password, is_admin))
            session.commit()
        _write_credentials(Path(credential_path), accounts)
    finally:
        database_engine.dispose()
    return tuple(accounts)


def _write_credentials(
    path: Path,
    accounts: list[tuple[str, str, bool]],
) -> None:
    """Ghi thông tin tài khoản qua file tạm với quyền POSIX 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write("Tài khoản demo OTTv2 - chỉ dùng trên môi trường local\n")
            output.write("Tạo lại seed sẽ xoay vòng các mật khẩu này.\n\n")
            for student_id, password, is_admin in accounts:
                role = "admin" if is_admin else "thí sinh"
                output.write(f"{role}\t{student_id}\t{password}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    """Yêu cầu xác nhận rõ trước khi ghi tài khoản thử nghiệm vào database."""
    parser = argparse.ArgumentParser(description="Tạo tài khoản OTTv2 cho kiểm thử local")
    parser.add_argument(
        "--enable-demo-accounts",
        action="store_true",
        help="Cho phép tạo/xoay vòng sáu tài khoản demo trong SQLite local",
    )
    arguments = parser.parse_args()
    if not arguments.enable_demo_accounts:
        parser.error("Thêm --enable-demo-accounts để xác nhận tạo tài khoản demo local")
    accounts = seed_demo_accounts()
    print(f"Đã ghi thông tin đăng nhập vào {config.STORAGE_DIR / 'demo-accounts.txt'}")
    for student_id, password, is_admin in accounts:
        print(f"{'admin' if is_admin else 'thí sinh'}\t{student_id}\t{password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())