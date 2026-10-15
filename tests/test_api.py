"""Kiểm thử API FastAPI về quyền riêng tư, xác thực và khóa nộp."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

import app as app_module
import config
from app import create_app, create_user
from db import create_database_engine, initialize_database
from engine import OTTGame
from models import DailyRank, Game, ReplayAccess, Submission, SystemSetting, TournamentRun
from replays import ReplayStore
from sandbox import BotExecutionResult


PASSWORD = "correct-horse-battery"
TIMEZONE = ZoneInfo(config.TIMEZONE_NAME)


def make_client(
    tmp_path: Path,
    tournament_trigger=None,
    publish_trigger=None,
) -> tuple[TestClient, object, Path, Path, Path]:
    """Tạo ứng dụng và ba tài khoản với tài nguyên riêng cho một test."""
    database_path = tmp_path / "api.sqlite"
    database_engine = create_database_engine(f"sqlite:///{database_path.as_posix()}")
    initialize_database(database_engine)
    with Session(database_engine) as session:
        for student_id in ("alpha", "bravo", "charlie"):
            create_user(session, student_id, PASSWORD, is_admin=student_id == "charlie")

    submission_root = tmp_path / "storage" / "submissions"
    replay_root = tmp_path / "storage" / "replays"
    leaderboard_root = tmp_path / "static" / "leaderboards"
    application = create_app(
        database_engine=database_engine,
        submission_directory=submission_root,
        replay_directory=replay_root,
        leaderboard_directory=leaderboard_root,
        secret_key="unit-test-secret-key-with-enough-entropy",
        tournament_trigger=tournament_trigger,
        publish_trigger=publish_trigger,
        initialize_schema=False,
    )
    application.state.clock = lambda: datetime(2026, 10, 15, 12, 0, tzinfo=TIMEZONE)
    return TestClient(application), database_engine, submission_root, replay_root, leaderboard_root


def login(client: TestClient, student_id: str) -> str:
    """Đăng nhập và trả token CSRF để dùng cho request ghi."""
    response = client.post(
        "/api/auth/login",
        json={"student_id": student_id, "password": PASSWORD},
    )
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    return response.json()["csrf_token"]


def test_hosted_mode_rejects_ephemeral_sqlite_database(monkeypatch) -> None:
    monkeypatch.setattr(config, "REQUIRE_REMOTE_SANDBOX", True)
    database_engine = create_database_engine("sqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            create_app(database_engine=database_engine, initialize_schema=False)
    finally:
        database_engine.dispose()


def test_demo_mode_rejects_remote_sandbox_and_object_storage(monkeypatch) -> None:
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(config, "REQUIRE_REMOTE_SANDBOX", False)
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    monkeypatch.setattr(config, "SANDBOX_TOKEN", "")
    monkeypatch.setenv("OTTV2_S3_ENDPOINT", "https://storage.example.invalid")
    with pytest.raises(RuntimeError, match="external object storage"):
        create_app(database_engine=create_database_engine("sqlite:///:memory:"))

    monkeypatch.delenv("OTTV2_S3_ENDPOINT")
    monkeypatch.setattr(config, "SANDBOX_URL", "https://worker.example.invalid")
    with pytest.raises(RuntimeError, match="remote sandbox"):
        create_app(database_engine=create_database_engine("sqlite:///:memory:"))


def test_pages_and_static_assets_are_served(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        pages = {
            "/": ("dashboard.html", "leaderboard-rows"),
            "/submit": ("submit.html", "upload-form"),
            "/profile": ("profile.html", "replay-list"),
            "/admin": ("admin.html", "admin-invite-form"),
        }
        stylesheet = client.get("/static/app.css")
        script = client.get("/static/app.js")

        for path, (template, expected_id) in pages.items():
            page = client.get(path)
            assert page.status_code == 200
            assert 'lang="vi"' in page.text
            assert f'data-template="{template}"' in page.text
            assert f'id="{expected_id}"' in page.text
            if path == "/admin":
                assert "Đăng nhập bằng tài khoản quản trị" in page.text
        assert stylesheet.status_code == 200
        assert "--jade" in stylesheet.text
        assert script.status_code == 200
        assert "loadLeaderboard" in script.text
    finally:
        client.close()
        database_engine.dispose()


def test_login_sets_http_only_session_and_logout_requires_csrf(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        csrf = login(client, "alpha")

        rejected = client.post("/api/auth/logout")
        accepted = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})

        assert rejected.status_code == 403
        assert accepted.status_code == 200
        assert accepted.json() == {"ok": True}
    finally:
        client.close()
        database_engine.dispose()


def test_demo_bootstrap_admin_is_created_once_and_never_resets_password(
    monkeypatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "bootstrap.sqlite"
    database_engine = create_database_engine(f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv(config.BOOTSTRAP_ADMIN_ID_ENV, "demo_admin")
    monkeypatch.setenv(config.BOOTSTRAP_ADMIN_PASSWORD_ENV, "first-bootstrap-password")
    application = create_app(
        database_engine=database_engine,
        submission_directory=tmp_path / "submissions",
        replay_directory=tmp_path / "replays",
        leaderboard_directory=tmp_path / "leaderboards",
        secret_key="unit-test-secret-key-with-enough-entropy",
    )
    try:
        with TestClient(application):
            pass
        with Session(database_engine) as session:
            original = session.get(app_module.User, "demo_admin")
            assert original is not None
            assert original.is_admin
            original_hash = original.password_hash

        monkeypatch.setenv(config.BOOTSTRAP_ADMIN_PASSWORD_ENV, "short")
        with TestClient(application):
            pass
        with Session(database_engine) as session:
            existing = session.get(app_module.User, "demo_admin")
            assert existing is not None
            assert existing.is_admin
            assert existing.password_hash == original_hash
            assert app_module.password_hasher.verify(
                existing.password_hash, "first-bootstrap-password"
            )
    finally:
        database_engine.dispose()


def test_admin_can_create_participant_account_but_non_admin_cannot(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        alpha_csrf = login(client, "alpha")
        denied = client.post(
            "/admin/accounts",
            json={"student_id": "new_bot", "password": "bot-password-8"},
            headers={"X-CSRF-Token": alpha_csrf},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ADMIN_REQUIRED"

        admin_csrf = login(client, "charlie")
        missing_csrf = client.post(
            "/admin/accounts",
            json={"student_id": "new_bot", "password": "bot-password-8"},
        )
        assert missing_csrf.status_code == 403

        invited = client.post(
            "/admin/accounts",
            json={"student_id": "new_bot", "password": "bot-password-8"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert invited.status_code == 200
        credentials = invited.json()
        assert credentials["student_id"] == "new_bot"
        assert set(credentials) == {"student_id"}
        assert client.get("/admin/control").status_code == 200
        assert client.post(
            "/api/auth/login",
            json={
                "student_id": credentials["student_id"],
                "password": "bot-password-8",
            },
        ).status_code == 200

        admin_csrf = login(client, "charlie")
        duplicate = client.post(
            "/admin/accounts",
            json={"student_id": "new_bot", "password": "bot-password-8"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert duplicate.status_code == 409
        invalid = client.post(
            "/admin/accounts",
            json={"student_id": "not valid", "password": "bot-password-8"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert invalid.status_code == 422
        weak_password = client.post(
            "/admin/accounts",
            json={"student_id": "weak_bot", "password": "1234567"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert weak_password.status_code == 422
    finally:
        client.close()
        database_engine.dispose()


def test_authenticated_account_without_submission_remains_logged_in(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        login(client, "alpha")
        response = client.get("/api/my/results")

        assert response.status_code == 200
        assert response.json() == {
            "student_id": "alpha",
            "is_admin": False,
            "results": [],
        }
    finally:
        client.close()
        database_engine.dispose()


def test_private_submission_download_never_accepts_another_student_id(
    tmp_path: Path,
) -> None:
    client, database_engine, submission_root, _, _ = make_client(tmp_path)
    try:
        alpha_content = b"def make_move(board, valid_moves, player): return valid_moves[0]"
        bravo_content = b"def make_move(board, valid_moves, player): return valid_moves[-1]"
        alpha_version = submission_root / "alpha" / "version_1.py"
        bravo_version = submission_root / "bravo" / "version_1.py"
        alpha_version.parent.mkdir(parents=True)
        bravo_version.parent.mkdir(parents=True)
        alpha_version.write_bytes(alpha_content)
        bravo_version.write_bytes(bravo_content)
        with Session(database_engine) as session:
            session.add_all(
                [
                    Submission(
                        student_id="alpha",
                        version=1,
                        sha256="a" * 64,
                        file_path=str(alpha_version),
                        valid=True,
                        submitted_at=datetime(2026, 10, 15, 10, 0, tzinfo=TIMEZONE),
                    ),
                    Submission(
                        student_id="bravo",
                        version=1,
                        sha256="b" * 64,
                        file_path=str(bravo_version),
                        valid=True,
                        submitted_at=datetime(2026, 10, 15, 10, 0, tzinfo=TIMEZONE),
                    ),
                ]
            )
            session.commit()

        login(client, "alpha")
        metadata = client.get("/api/my/submission?student_id=bravo")
        download = client.get("/api/my/submission/file?student_id=bravo")
        direct_path = client.get("/api/my/submission/bravo")
        static_path = client.get("/storage/submissions/bravo.py")

        assert metadata.status_code == 200
        assert metadata.json()["student_id"] == "alpha"
        assert download.status_code == 200
        assert download.content == alpha_content
        assert bravo_content not in download.content
        assert direct_path.status_code == 404
        assert static_path.status_code == 404
    finally:
        client.close()
        database_engine.dispose()


def test_user_cannot_read_replay_not_in_their_access_quota(tmp_path: Path) -> None:
    client, database_engine, _, replay_root, _ = make_client(tmp_path)
    try:
        game = OTTGame()
        move = game.get_valid_moves(1)[0]
        encoded = bytes(
            (
                move[0][0] * 9 + move[0][1],
                move[1][0] * 9 + move[1][1],
            )
        )
        replay_path = replay_root / "run-alpha.bin"
        offset, length = ReplayStore(replay_path).append(encoded)
        with Session(database_engine) as session:
            session.add(
                TournamentRun(
                    run_id="run-alpha",
                    tournament_id="tournament-test",
                    round_no=1,
                    day_no=1,
                    status="COMPLETED",
                    partial=False,
                )
            )
            session.add(
                Game(
                    game_id="123e4567-e89b-42d3-a456-426614174000",
                    run_id="run-alpha",
                    day_no=1,
                    stage=1,
                    round_in_stage=1,
                    p1_id="bravo",
                    p2_id="charlie",
                    result="P1",
                    win_reason="Thắng kiểm thử",
                    plies=1,
                    captured_p1=0,
                    captured_p2=0,
                    replay_offset=offset,
                    replay_length=length,
                    replay_file=replay_path.name,
                    metadata_json={},
                )
            )
            session.flush()
            session.add(
                ReplayAccess(
                    student_id="bravo",
                    game_id="123e4567-e89b-42d3-a456-426614174000",
                    run_id="run-alpha",
                    order_no=1,
                )
            )
            session.commit()

        login(client, "alpha")
        forbidden = client.get("/api/replay/123e4567-e89b-42d3-a456-426614174000")
        listing = client.get("/api/my/replays")

        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "REPLAY_FORBIDDEN"
        assert listing.json()["replays"] == []

        login(client, "bravo")
        own_listing = client.get("/api/my/replays?day=1")
        own_replay = client.get("/api/replay/123e4567-e89b-42d3-a456-426614174000")
        assert len(own_listing.json()["replays"]) == 1
        assert own_listing.json()["replays"][0]["opponent"] == "Đối thủ"
        assert "moves" not in own_listing.json()["replays"][0]
        assert own_replay.status_code == 200
        assert len(own_replay.json()["frames"]) == 2
    finally:
        client.close()
        database_engine.dispose()


def test_admin_lock_blocks_submission_at_any_hour(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        admin_csrf = login(client, "charlie")
        locked = client.post(
            "/admin/submissions/lock",
            json={"locked": True},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert locked.status_code == 200

        csrf = login(client, "alpha")
        client.app.state.clock = lambda: datetime(2026, 10, 15, 2, 0, tzinfo=TIMEZONE)

        response = client.post(
            "/api/submit",
            files={"file": ("bot.py", b"def make_move(board, valid_moves, player): return valid_moves[0]")},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "SUBMISSION_LOCKED"
    finally:
        client.close()
        database_engine.dispose()


def test_valid_upload_is_saved_atomically_with_hash_and_version(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, database_engine, submission_root, _, _ = make_client(tmp_path)
    monkeypatch.setattr(
        app_module,
        "run_bot",
        lambda *args, **kwargs: BotExecutionResult(args[2][0], None, 0.01),
    )
    try:
        csrf = login(client, "alpha")
        source = b"def make_move(board, valid_moves, player): return valid_moves[0]"
        response = client.post(
            "/api/submit",
            files={"file": ("bot.py", source)},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 200
        assert response.json()["version"] == 1
        assert len(response.json()["sha256"]) == 64
        assert (submission_root / "alpha.py").read_bytes() == source
        assert (submission_root / "alpha" / "version_1.py").read_bytes() == source
    finally:
        client.close()
        database_engine.dispose()


def test_leaderboard_route_serves_static_json_and_etag_without_database_query(
    tmp_path: Path,
) -> None:
    client, database_engine, _, _, leaderboard_root = make_client(tmp_path)
    try:
        leaderboard_root.mkdir(parents=True)
        (leaderboard_root / "daily.json").write_text('{"entries":[]}', encoding="utf-8")
        statements: list[str] = []

        def capture_sql(*args) -> None:
            statements.append(args[2])

        event.listen(database_engine, "before_cursor_execute", capture_sql)
        response = client.get("/api/leaderboard/daily")
        not_modified = client.get(
            "/api/leaderboard/daily",
            headers={"If-None-Match": response.headers["etag"]},
        )
        event.remove(database_engine, "before_cursor_execute", capture_sql)

        assert response.status_code == 200
        assert response.json() == {"entries": []}
        assert response.headers["cache-control"].startswith("public")
        assert not_modified.status_code == 304
        assert statements == []
    finally:
        client.close()
        database_engine.dispose()


def test_status_does_not_lock_by_clock(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        client.app.state.clock = lambda: datetime(2026, 10, 15, 5, 59, tzinfo=TIMEZONE)
        early = client.get("/api/status").json()
        client.app.state.clock = lambda: datetime(2026, 10, 15, 6, 0, tzinfo=TIMEZONE)
        late = client.get("/api/status").json()

        assert early["status"] == "OPEN"
        assert early["submissions_locked"] is False
        assert late["status"] == "OPEN"
        assert late["day_no"] == 1
        assert late["round_no"] == 1
    finally:
        client.close()
        database_engine.dispose()


def test_status_reports_running_then_published_from_run_state(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        with Session(database_engine) as session:
            session.add(
                TournamentRun(
                    run_id="run-current",
                    tournament_id="tournament-test",
                    round_no=1,
                    day_no=1,
                    status="RUNNING",
                    partial=False,
                    started_at=datetime(2026, 10, 15, 1, 0, tzinfo=TIMEZONE),
                )
            )
            session.commit()
        client.app.state.clock = lambda: datetime(2026, 10, 15, 2, 0, tzinfo=TIMEZONE)

        running = client.get("/api/status").json()
        with Session(database_engine) as session:
            run = session.get(TournamentRun, "run-current")
            assert run is not None
            run.status = "COMPLETED"
            run.completed_at = datetime(2026, 10, 15, 5, 0, tzinfo=TIMEZONE)
            session.add(
                SystemSetting(
                    key="published_day",
                    value=1,
                    updated_at=datetime(2026, 10, 15, 6, 0, tzinfo=TIMEZONE),
                )
            )
            session.commit()
        client.app.state.clock = lambda: datetime(2026, 10, 15, 6, 0, tzinfo=TIMEZONE)
        published = client.get("/api/status").json()

        assert running["status"] == "RUNNING"
        assert published["status"] == "PUBLISHED"
    finally:
        client.close()
        database_engine.dispose()


def test_admin_can_start_scoring_and_publish_only_with_callbacks(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(
        tmp_path,
        tournament_trigger=lambda day: f"run-day-{day}",
        publish_trigger=lambda day: {"day_no": day, "entries": 5, "partial": False},
    )
    try:
        csrf = login(client, "charlie")
        lock_response = client.post(
            "/admin/submissions/lock",
            json={"locked": True},
            headers={"X-CSRF-Token": csrf},
        )
        score_response = client.post(
            "/admin/score?day=1",
            headers={"X-CSRF-Token": csrf},
        )
        publish_response = client.post(
            "/admin/publish?day=1",
            headers={"X-CSRF-Token": csrf},
        )

        assert lock_response.status_code == 200
        assert score_response.status_code == 200
        assert score_response.json()["run_id"] == "run-day-1"
        assert publish_response.status_code == 200
        assert publish_response.json()["status"] == "PUBLISHED"
    finally:
        client.close()
        database_engine.dispose()


def test_non_admin_cannot_change_submission_lock(tmp_path: Path) -> None:
    client, database_engine, _, _, _ = make_client(tmp_path)
    try:
        csrf = login(client, "alpha")
        response = client.post(
            "/admin/submissions/lock",
            json={"locked": True},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ADMIN_REQUIRED"
    finally:
        client.close()
        database_engine.dispose()