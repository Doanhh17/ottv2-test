"""Kiểm thử seeder tài khoản demo bị giới hạn vào SQLite."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import create_database_engine
from models import User
from seed_demo_accounts import seed_demo_accounts


def test_seed_creates_one_admin_and_five_students(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'demo.sqlite').as_posix()}"
    credential_path = tmp_path / "private" / "accounts.txt"

    accounts = seed_demo_accounts(database_url, credential_path)

    assert len(accounts) == 6
    assert sum(is_admin for _, _, is_admin in accounts) == 1
    assert credential_path.is_file()
    with Session(create_database_engine(database_url)) as session:
        users = session.scalars(select(User)).all()
        assert len(users) == 6
        assert sum(user.is_admin for user in users) == 1
        assert all(user.password_hash.startswith("$argon2") for user in users)


def test_seed_refuses_postgresql_database() -> None:
    import pytest

    with pytest.raises(ValueError, match="SQLite local"):
        seed_demo_accounts("postgresql+psycopg://user:pass@localhost/db", "accounts.txt")