"""Kiểm thử schema SQLAlchemy và khởi tạo cơ sở dữ liệu."""

from sqlalchemy import inspect

from db import create_database_engine, initialize_database


def test_sqlite_schema_creates_required_tables() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    try:
        initialize_database(engine)

        tables = set(inspect(engine).get_table_names())

        assert {
            "users",
            "submissions",
            "runs",
            "games",
            "matches",
            "daily_ranks",
            "replay_access",
        } <= tables
    finally:
        engine.dispose()