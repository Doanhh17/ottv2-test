"""Tính và xuất bảng xếp hạng OTTv2 theo ngày, vòng và giải."""

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

import config


LeaderboardKind = Literal["daily", "round", "tournament"]


@dataclass(frozen=True, slots=True)
class DailyRankRecord:
    """Rank ngày đã chấm của một bot."""

    student_id: str
    day_no: int
    rank: int
    top32: bool = False
    absent: bool = False


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """Một dòng công khai trong bảng xếp hạng."""

    student_id: str
    rank: int
    average_rank: float
    days_played: int
    top32_days: int
    latest_rank: int
    absent: bool
    trend: str


def build_leaderboard(
    student_ids: Iterable[str],
    records: Iterable[DailyRankRecord],
    expected_days: Sequence[int],
    kind: LeaderboardKind,
    round_days: Sequence[int] | None = None,
    absent_rank: int = config.ABSENT_DAILY_RANK,
) -> tuple[LeaderboardEntry, ...]:
    """Tổng hợp BXH; ngày vắng được tính rank cấu hình, không tính ngày đã đấu."""
    students = sorted(set(student_ids))
    if not students:
        return ()
    days = sorted(set(expected_days))
    if absent_rank < 1:
        raise ValueError("absent_rank phải lớn hơn 0")
    selected_days = set(days)
    if kind == "round":
        selected_days &= set(round_days or ())
    elif kind == "daily":
        selected_days = {max(days)} if days else set()

    all_records: dict[tuple[str, int], DailyRankRecord] = {}
    for record in records:
        if record.student_id in students:
            all_records[(record.student_id, record.day_no)] = record

    rank_by_student_day = {
        key: record for key, record in all_records.items() if record.day_no in selected_days
    }

    latest_day = max(selected_days) if selected_days else None
    computed: list[tuple[tuple[float, int, int, str], LeaderboardEntry]] = []
    for student_id in students:
        values = [
            rank_by_student_day.get((student_id, day_no))
            for day_no in sorted(selected_days)
        ]
        filled_ranks = [
            record.rank if record is not None else absent_rank for record in values
        ]
        latest_record = (
            rank_by_student_day.get((student_id, latest_day))
            if latest_day is not None
            else None
        )
        latest_rank = latest_record.rank if latest_record is not None else absent_rank
        previous_days = [day_no for day_no in days if latest_day is not None and day_no < latest_day]
        previous_record = (
            all_records.get((student_id, max(previous_days))) if previous_days else None
        )
        trend = (
            "up"
            if latest_record is not None
            and previous_record is not None
            and latest_rank < previous_record.rank
            else "down"
            if latest_record is not None
            and previous_record is not None
            and latest_rank > previous_record.rank
            else "flat"
            if latest_record is not None and previous_record is not None
            else "unknown"
        )
        days_played = sum(record is not None and not record.absent for record in values)
        top32_days = sum(
            record is not None and record.top32 for record in values
        )
        average = sum(filled_ranks) / len(filled_ranks) if filled_ranks else float(absent_rank)
        is_absent = latest_record is None or latest_record.absent
        entry = LeaderboardEntry(
            student_id,
            0,
            average,
            days_played,
            top32_days,
            latest_rank,
            is_absent,
            trend,
        )
        computed.append(
            ((average, -top32_days, latest_rank, student_id), entry)
        )

    computed.sort(key=lambda pair: pair[0])
    return tuple(
        LeaderboardEntry(
            entry.student_id,
            rank,
            entry.average_rank,
            entry.days_played,
            entry.top32_days,
            entry.latest_rank,
            entry.absent,
            entry.trend,
        )
        for rank, (_, entry) in enumerate(computed, start=1)
    )


def leaderboard_payload(
    entries: Sequence[LeaderboardEntry],
    generated_at: datetime | None = None,
    day_no: int | None = None,
    round_no: int | None = None,
    partial: bool = False,
) -> dict[str, object]:
    """Chuyển BXH thành JSON chỉ chứa dữ liệu công khai."""
    timezone = ZoneInfo(config.TIMEZONE_NAME)
    timestamp = generated_at or datetime.now(timezone)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone)
    return {
        "meta": {
            "generated_at": timestamp.astimezone(timezone).isoformat(),
            "day_no": day_no,
            "round_no": round_no,
            "partial": partial,
        },
        "entries": [
            {
                "student_id": entry.student_id,
                "rank": entry.rank,
                "average_rank": round(entry.average_rank, 4),
                "days_played": entry.days_played,
                "top32_days": entry.top32_days,
                "latest_rank": entry.latest_rank,
                "absent": entry.absent,
                "trend": entry.trend,
            }
            for entry in entries
        ],
    }


def write_json_atomic(path: str | Path, payload: object) -> Path:
    """Ghi JSON qua tệp tạm cùng thư mục rồi thay thế nguyên tử."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(encoded)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return target


def export_leaderboards(
    daily: object,
    round_payloads: Mapping[int, object],
    tournament: object,
    meta: object,
    directory: str | Path = config.LEADERBOARD_STORAGE_DIR,
) -> tuple[Path, ...]:
    """Xuất ba loại BXH và metadata bằng thay thế atomic từng tệp."""
    target_dir = Path(directory)
    outputs = [write_json_atomic(target_dir / "daily.json", daily)]
    outputs.extend(
        write_json_atomic(target_dir / f"round_{round_no}.json", payload)
        for round_no, payload in sorted(round_payloads.items())
    )
    outputs.append(write_json_atomic(target_dir / "tournament.json", tournament))
    outputs.append(write_json_atomic(target_dir / "meta.json", meta))
    return tuple(outputs)
