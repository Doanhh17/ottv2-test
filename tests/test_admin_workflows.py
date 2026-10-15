"""Kiểm thử luồng admin chấm nền và công bố BXH."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from admin_workflows import AdminWorkflows
from db import create_database_engine, initialize_database
from models import DailyRank, Game, ReplayAccess, TournamentRun, User
from replays import replay_quota


def test_scoring_run_persists_unique_ranks_replays_and_publish_files(
    tmp_path: Path,
) -> None:
    database_engine = create_database_engine(f"sqlite:///{(tmp_path / 'admin.sqlite').as_posix()}")
    initialize_database(database_engine)
    sessions = sessionmaker(bind=database_engine, expire_on_commit=False)
    fixed_now = datetime(2026, 10, 15, 12, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    with Session(database_engine) as session:
        session.add_all(
            [
                User(
                    student_id=f"bot{index:02}",
                    password_hash="test-hash",
                    is_admin=False,
                    created_at=fixed_now,
                )
                for index in range(1, 5)
            ]
        )
        session.commit()

    workflow = AdminWorkflows(
        sessions,
        tmp_path / "storage" / "submissions",
        tmp_path / "storage" / "replays",
        tmp_path / "static" / "leaderboards",
        clock=lambda: fixed_now,
    )

    run_id = workflow.start_scoring(1)

    assert workflow.wait_for_scoring(run_id, timeout_seconds=20)
    with Session(database_engine) as session:
        run = session.get(TournamentRun, run_id)
        ranks = session.scalars(select(DailyRank).where(DailyRank.day_no == 1)).all()
        games = session.scalars(select(Game).where(Game.run_id == run_id)).all()
        assert run is not None
        assert run.status == "COMPLETED"
        assert set(row.rank for row in ranks) == {1, 2, 3, 4}
        assert len(games) > 0

    published = workflow.publish_day(1)

    assert published == {"day_no": 1, "round_no": 1, "entries": 4, "partial": False}
    assert {
        "daily.json",
        "round_1.json",
        "tournament.json",
        "meta.json",
    } <= {path.name for path in (tmp_path / "static" / "leaderboards").iterdir()}
    with Session(database_engine) as session:
        assert session.get(TournamentRun, run_id).status == "PUBLISHED"
        games = session.scalars(select(Game).where(Game.run_id == run_id)).all()
        accesses = session.scalars(
            select(ReplayAccess).where(ReplayAccess.run_id == run_id)
        ).all()
        games_by_id = {game.game_id: game for game in games}
        assert accesses
        for access in accesses:
            game = games_by_id[access.game_id]
            assert access.student_id in (game.p1_id, game.p2_id)

        for student_id in {game.p1_id for game in games} | {
            game.p2_id for game in games
        }:
            played_games = [
                game
                for game in games
                if student_id in (game.p1_id, game.p2_id)
            ]
            selected_games = [
                access for access in accesses if access.student_id == student_id
            ]
            assert len(selected_games) == replay_quota(len(played_games))

    database_engine.dispose()