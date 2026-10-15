"""Các bảng SQLAlchemy lưu người dùng, giải đấu và replay OTTv2."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Lớp cơ sở cho các mô hình ORM."""


class User(Base):
    """Tài khoản thí sinh, không lưu mật khẩu dạng rõ."""

    __tablename__ = "users"

    student_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submissions: Mapped[list["Submission"]] = relationship(back_populates="user")


class Submission(Base):
    """Một phiên bản bài nộp và hash SHA-256 tương ứng."""

    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("student_id", "version", name="uq_submission_version"),
        Index("ix_submissions_student_time", "student_id", "submitted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.student_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user: Mapped[User] = relationship(back_populates="submissions")


class TournamentRun(Base):
    """Một lượt chấm theo ngày, có thể đánh dấu kết quả một phần."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tournament_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    day_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checkpoint_stage: Mapped[str | None] = mapped_column(String(64))
    checkpoint_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Match(Base):
    """Một cặp đấu ở benchmark, Swiss hoặc playoff."""

    __tablename__ = "matches"
    __table_args__ = (Index("ix_matches_run_stage_round", "run_id", "stage", "round_no"),)

    match_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    first_id: Mapped[str] = mapped_column(String(32), nullable=False)
    second_id: Mapped[str] = mapped_column(String(32), nullable=False)
    winner_id: Mapped[str | None] = mapped_column(String(32))
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Game(Base):
    """Metadata ván đấu và vị trí replay trong file nối tiếp."""

    __tablename__ = "games"
    __table_args__ = (
        Index("ix_games_run_stage_round", "run_id", "stage", "round_in_stage"),
        Index("ix_games_participants", "p1_id", "p2_id"),
    )

    game_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False)
    day_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    round_in_stage: Mapped[int] = mapped_column(Integer, nullable=False)
    p1_id: Mapped[str] = mapped_column(String(32), nullable=False)
    p2_id: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(8), nullable=False)
    win_reason: Mapped[str] = mapped_column(Text, nullable=False)
    plies: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_p1: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_p2: Mapped[int] = mapped_column(Integer, nullable=False)
    replay_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    replay_length: Mapped[int] = mapped_column(Integer, nullable=False)
    replay_file: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class DailyRank(Base):
    """Hạng ngày của một thí sinh trong một giải."""

    __tablename__ = "daily_ranks"
    __table_args__ = (
        Index("ix_daily_ranks_lookup", "tournament_id", "day_no", "rank"),
    )

    tournament_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.student_id"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top32: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    absent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ReplayAccess(Base):
    """Quyền xem có giới hạn của một thí sinh với một replay."""

    __tablename__ = "replay_access"
    __table_args__ = (
        UniqueConstraint("student_id", "game_id", name="uq_replay_access_student_game"),
        Index("ix_replay_access_student_day", "student_id", "run_id", "order_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("users.student_id"), nullable=False)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.game_id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), nullable=False)
    order_no: Mapped[int] = mapped_column(Integer, nullable=False)


class SystemSetting(Base):
    """Trạng thái điều hành có thể thay đổi mà không phụ thuộc giờ cố định."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
