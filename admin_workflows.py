"""Luồng chấm nền và công bố bảng xếp hạng do quản trị viên kích hoạt."""

from datetime import datetime
import logging
from pathlib import Path
import re
import threading
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

import config
from leaderboard import (
    DailyRankRecord,
    build_leaderboard,
    export_leaderboards,
    leaderboard_payload,
)
from models import DailyRank, Game, Submission, SystemSetting, TournamentRun, User
from object_storage import ObjectStorage
from replays import ReplayStore
from remote_sandbox import execute_game
from tournament_runner import BotEntrant, TournamentRunner


logger = logging.getLogger("ottv2.admin_workflows")
Clock = Callable[[], datetime]


class AdminWorkflows:
    """Điều phối chấm và xuất BXH bằng các bảng ORM hiện có."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        submission_directory: str | Path,
        replay_directory: str | Path,
        leaderboard_directory: str | Path,
        clock: Clock | None = None,
        object_storage: ObjectStorage | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.submission_directory = Path(submission_directory).resolve()
        self.replay_directory = Path(replay_directory).resolve()
        self.leaderboard_directory = Path(leaderboard_directory).resolve()
        self.clock = clock or (lambda: datetime.now(ZoneInfo(config.TIMEZONE_NAME)))
        self.object_storage = object_storage or ObjectStorage.from_environment()
        self._threads: dict[str, threading.Thread] = {}

    def start_scoring(self, day_no: int) -> str:
        """Tạo run ở trạng thái RUNNING rồi chấm bất đồng bộ."""
        now = self.clock()
        round_no = min((day_no - 1) // config.ROUND_DAYS + 1, 5)
        run_id = str(uuid.uuid4())
        with self.session_factory() as session:
            active_run = session.scalar(
                select(TournamentRun)
                .where(
                    TournamentRun.day_no == day_no,
                    TournamentRun.status == "RUNNING",
                )
                .limit(1)
            )
            if active_run is not None:
                raise RuntimeError("Đang có lượt chấm chạy cho ngày này")
            session.add(
                TournamentRun(
                    run_id=run_id,
                    tournament_id=config.TOURNAMENT_ID,
                    round_no=round_no,
                    day_no=day_no,
                    status="RUNNING",
                    partial=False,
                    started_at=now,
                )
            )
            session.commit()

        thread = threading.Thread(
            target=self._score_day,
            args=(run_id, day_no, round_no, now),
            name=f"ottv2-score-{day_no}",
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()
        return run_id

    def wait_for_scoring(self, run_id: str, timeout_seconds: float | None = None) -> bool:
        """Chờ một run nền kết thúc; hữu ích cho test và tác vụ vận hành."""
        thread = self._threads.get(run_id)
        if thread is None:
            return True
        thread.join(timeout_seconds)
        return not thread.is_alive()

    def publish_day(self, day_no: int) -> dict[str, Any]:
        """Dựng daily/round/tournament JSON rồi đánh dấu lượt đã công bố."""
        with self.session_factory() as session:
            run = session.scalar(
                select(TournamentRun)
                .where(
                    TournamentRun.day_no == day_no,
                    TournamentRun.tournament_id == config.TOURNAMENT_ID,
                    TournamentRun.status.in_(("COMPLETED", "PARTIAL", "PUBLISHED")),
                )
                .order_by(desc(TournamentRun.started_at))
                .limit(1)
            )
            if run is None:
                raise RuntimeError("Chưa có lượt chấm hoàn tất để công bố")

            users = session.scalars(
                select(User).where(User.is_admin.is_(False)).order_by(User.student_id)
            ).all()
            ranks = session.scalars(
                select(DailyRank)
                .where(DailyRank.tournament_id == config.TOURNAMENT_ID)
                .order_by(DailyRank.day_no, DailyRank.student_id)
            ).all()
            records = [
                DailyRankRecord(
                    rank.student_id,
                    rank.day_no,
                    rank.rank,
                    rank.top32,
                    rank.absent,
                )
                for rank in ranks
            ]
            student_ids = [user.student_id for user in users]
            round_start = (run.round_no - 1) * config.ROUND_DAYS + 1
            round_days = list(range(round_start, day_no + 1))
            season_days = list(range(1, day_no + 1))
            daily_entries = build_leaderboard(
                student_ids, records, [day_no], "daily"
            )
            round_entries = build_leaderboard(
                student_ids,
                records,
                season_days,
                "round",
                round_days=round_days,
            )
            season_entries = build_leaderboard(
                student_ids, records, season_days, "tournament"
            )
            generated_at = self.clock()
            meta = {
                "generated_at": generated_at.isoformat(),
                "day_no": day_no,
                "round_no": run.round_no,
                "partial": run.partial,
            }
            export_leaderboards(
                leaderboard_payload(
                    daily_entries, generated_at, day_no, run.round_no, run.partial
                ),
                {
                    run.round_no: leaderboard_payload(
                        round_entries, generated_at, day_no, run.round_no, run.partial
                    )
                },
                leaderboard_payload(
                    season_entries, generated_at, day_no, run.round_no, run.partial
                ),
                meta,
                self.leaderboard_directory,
            )
            for filename in (
                "daily.json",
                "tournament.json",
                f"round_{run.round_no}.json",
            ):
                self.object_storage.put_path(
                    self.leaderboard_directory / filename,
                    self.leaderboard_directory,
                    "application/json",
                )
            run.status = "PUBLISHED"
            session.merge(
                SystemSetting(
                    key="published_day",
                    value=day_no,
                    updated_at=generated_at,
                )
            )
            session.commit()
            return {
                "day_no": day_no,
                "round_no": run.round_no,
                "entries": len(daily_entries),
                "partial": run.partial,
            }

    def _score_day(
        self,
        run_id: str,
        day_no: int,
        round_no: int,
        started_at: datetime,
    ) -> None:
        """Chấm danh sách thí sinh, lưu replay/rank và trạng thái run."""
        try:
            with self.session_factory() as session:
                users = session.scalars(
                    select(User)
                    .where(User.is_admin.is_(False))
                    .order_by(User.student_id)
                ).all()
                entrants = self._load_entrants(session, users)
            if len(entrants) < 2:
                raise RuntimeError("Cần ít nhất hai tài khoản thí sinh để chấm")

            gatekeeper_cutoff = min(
                config.GATEKEEPER_CUTOFF,
                len(entrants) // 2 * 2,
            )
            swiss_rounds = min(config.SWISS_ROUNDS, gatekeeper_cutoff - 1)
            playoff_cutoff = 1 << min(
                config.PLAYOFF_CUTOFF.bit_length() - 1,
                gatekeeper_cutoff.bit_length() - 1,
            )
            playoff_cutoff = max(2, playoff_cutoff)
            runner = TournamentRunner(
                run_id,
                workers=config.ADMIN_SCORE_WORKERS,
                game_executor=execute_game if config.SANDBOX_URL else None,
                checkpoint=lambda stage, state: self._save_checkpoint(run_id, stage),
                parallel_game_executor=bool(config.SANDBOX_URL),
            )
            summary = runner.run(
                entrants,
                gatekeeper_cutoff=gatekeeper_cutoff,
                swiss_rounds=max(1, swiss_rounds),
                playoff_cutoff=playoff_cutoff,
            )
            entrant_by_id = {entrant.student_id: entrant for entrant in entrants}
            gatekeeper_points = {
                standing.student_id: standing.points for standing in summary.gatekeeper
            }
            replay_filename = f"{run_id}.bin"
            replay_store = ReplayStore(self.replay_directory / replay_filename)
            with self.session_factory() as session:
                for game_result in summary.game_results:
                    metadata = game_result.metadata
                    replay_offset, replay_length = replay_store.append(game_result.moves_bytes)
                    stage, replay_round = self._stage_and_round(str(metadata["match_id"]))
                    session.add(
                        Game(
                            game_id=game_result.game_id,
                            run_id=run_id,
                            day_no=day_no,
                            stage=stage,
                            round_in_stage=replay_round,
                            p1_id=metadata["p1_id"],
                            p2_id=metadata["p2_id"],
                            result=metadata["result"],
                            win_reason=metadata["win_reason"],
                            plies=metadata["plies"],
                            captured_p1=metadata["captured_p1"],
                            captured_p2=metadata["captured_p2"],
                            replay_offset=replay_offset,
                            replay_length=replay_length,
                            replay_file=replay_filename,
                            metadata_json={
                                "match_id": metadata["match_id"],
                                "game_index": metadata["game_index"],
                            },
                        )
                    )
                self.object_storage.put_path(
                    replay_store.path,
                    self.replay_directory,
                    "application/octet-stream",
                )
                for student_id, rank in summary.ranks.items():
                    session.merge(
                        DailyRank(
                            tournament_id=config.TOURNAMENT_ID,
                            day_no=day_no,
                            student_id=student_id,
                            rank=rank,
                            points=gatekeeper_points.get(student_id, 0),
                            top32=rank <= config.PLAYOFF_CUTOFF,
                            absent=not entrant_by_id[student_id].source,
                        )
                    )
                run = session.get(TournamentRun, run_id)
                if run is not None:
                    run.status = "PARTIAL" if summary.partial else "COMPLETED"
                    run.partial = summary.partial
                    run.checkpoint_stage = "complete"
                    run.completed_at = self.clock()
                session.commit()
        except Exception:
            logger.exception("admin_scoring_failed", extra={"run_id": run_id})
            with self.session_factory() as session:
                run = session.get(TournamentRun, run_id)
                if run is not None:
                    run.status = "FAILED"
                    run.partial = True
                    run.checkpoint_stage = "failed"
                    run.completed_at = self.clock()
                    session.commit()

    def _load_entrants(self, session: Session, users: list[User]) -> list[BotEntrant]:
        """Đọc phiên bản hợp lệ mới nhất của từng tài khoản."""
        entrants: list[BotEntrant] = []
        now = self.clock()
        for user in users:
            submission = session.scalar(
                select(Submission)
                .where(
                    Submission.student_id == user.student_id,
                    Submission.valid.is_(True),
                    Submission.submitted_at <= now,
                )
                .order_by(desc(Submission.submitted_at), desc(Submission.version))
                .limit(1)
            )
            source = None
            submission_error = "Không có bài hợp lệ trước thời điểm chấm"
            if submission is not None:
                path = Path(submission.file_path).resolve()
                if self.submission_directory in path.parents:
                    try:
                        source = self.object_storage.read_path(
                            path, self.submission_directory
                        ).decode("utf-8")
                    except FileNotFoundError:
                        pass
                    else:
                        submission_error = None
            entrants.append(
                BotEntrant(
                    user.student_id,
                    source,
                    submission_error=submission_error,
                )
            )
        return entrants

    def _save_checkpoint(self, run_id: str, stage: str) -> None:
        """Lưu mốc giai đoạn mới nhất để admin theo dõi lượt chấm."""
        with self.session_factory() as session:
            run = session.get(TournamentRun, run_id)
            if run is not None:
                run.checkpoint_stage = stage
                run.checkpoint_data = {"stage": stage}
                session.commit()

    @staticmethod
    def _stage_and_round(match_id: str) -> tuple[int, int]:
        """Đọc stage/vòng từ định danh trận chuẩn hóa của TournamentRunner."""
        parts = match_id.split(":")
        if parts[0] == "gatekeeper":
            return 1, 1
        if parts[0] == "swiss":
            return 2, int(parts[1])
        match = re.match(r"playoff:(?:winners|losers):(\d+)", match_id)
        if match:
            return 3, int(match.group(1))
        return 3, config.SWISS_ROUNDS + 1