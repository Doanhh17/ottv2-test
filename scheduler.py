"""Lập lịch chấm giải và dọn replay theo múi giờ Việt Nam."""

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config


TournamentJob = Callable[[datetime, datetime], None]
CleanupJob = Callable[[], None]


def build_scheduler(
    tournament_job: TournamentJob,
    cleanup_job: CleanupJob,
) -> BackgroundScheduler:
    """Tạo scheduler với chấm lúc 01:00 và dọn dữ liệu lúc 06:30."""
    timezone = ZoneInfo(config.TIMEZONE_NAME)
    scheduler = BackgroundScheduler(timezone=timezone, job_defaults={"coalesce": True})
    scheduler.add_job(
        _run_daily_tournament,
        CronTrigger(hour=config.TOURNAMENT_START_HOUR, minute=0, timezone=timezone),
        id="ottv2_daily_tournament",
        args=[tournament_job, timezone],
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        cleanup_job,
        CronTrigger(hour=config.LEADERBOARD_PUBLISH_HOUR, minute=30, timezone=timezone),
        id="ottv2_replay_cleanup",
        replace_existing=True,
        max_instances=1,
    )
    return scheduler


def start_scheduler(
    tournament_job: TournamentJob,
    cleanup_job: CleanupJob,
) -> BackgroundScheduler:
    """Khởi chạy scheduler nền và trả instance cho ứng dụng quản lý."""
    scheduler = build_scheduler(tournament_job, cleanup_job)
    scheduler.start()
    return scheduler


def _run_daily_tournament(
    tournament_job: TournamentJob,
    timezone: ZoneInfo,
) -> None:
    """Truyền thời điểm bắt đầu và deadline cứng 05:00 cho runner."""
    started_at = datetime.now(timezone)
    deadline = started_at.replace(
        hour=config.TOURNAMENT_END_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    tournament_job(started_at, deadline)
