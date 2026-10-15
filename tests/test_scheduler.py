"""Kiểm thử lịch chấm và timezone của scheduler."""

from zoneinfo import ZoneInfo

from scheduler import build_scheduler


def test_scheduler_jobs_use_vietnam_timezone_and_daily_hours() -> None:
    scheduler = build_scheduler(lambda started, deadline: None, lambda: None)
    try:
        timezone = ZoneInfo("Asia/Ho_Chi_Minh")
        tournament = scheduler.get_job("ottv2_daily_tournament")
        cleanup = scheduler.get_job("ottv2_replay_cleanup")

        assert scheduler.timezone == timezone
        assert tournament is not None
        assert cleanup is not None
        assert tournament.trigger.timezone == timezone
        assert cleanup.trigger.timezone == timezone
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)