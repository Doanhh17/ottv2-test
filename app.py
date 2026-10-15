"""API FastAPI của OTTv2 với xác thực, quyền riêng tư và khóa giờ nộp."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Generator
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Engine, desc, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.templating import Jinja2Templates

import config
from accounts import bootstrap_admin, create_user
from admin_workflows import AdminWorkflows
from db import create_database_engine, initialize_database
from engine import OTTGame
from models import DailyRank, Game, ReplayAccess, Submission, SystemSetting, TournamentRun, User
from object_storage import ObjectStorage
from replays import ReplayFormatError, ReplayStore, build_replay_frames, replay_quota
from sandbox import run_bot, scan_bot_source


logger = logging.getLogger("ottv2.api")
password_hasher = PasswordHasher()
TournamentTrigger = Callable[[int], str]
PublishTrigger = Callable[[int], dict[str, Any]]


class LoginRequest(BaseModel):
    """Thông tin xác thực đầu vào."""

    student_id: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class InviteParticipantRequest(BaseModel):
    """Student identifier for an admin-created participant account."""

    student_id: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=256)


class SubmissionLockRequest(BaseModel):
    """Trạng thái khóa nhận bài do quản trị viên chọn."""

    locked: bool


class RateLimiter:
    """Bộ giới hạn sliding-window theo khóa, an toàn giữa luồng trong process."""

    def __init__(self, maximum: int, window_seconds: int) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Ghi nhận yêu cầu và trả về có nằm trong hạn mức hay không."""
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            events = self._events[key]
            cutoff = timestamp - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.maximum:
                return False
            events.append(timestamp)
            return True


def create_app(
    database_engine: Engine | None = None,
    submission_directory: str | Path = config.SUBMISSION_STORAGE_DIR,
    replay_directory: str | Path = config.REPLAY_STORAGE_DIR,
    leaderboard_directory: str | Path = config.LEADERBOARD_STORAGE_DIR,
    secret_key: str = config.JWT_SECRET_KEY,
    tournament_trigger: TournamentTrigger | None = None,
    publish_trigger: PublishTrigger | None = None,
    initialize_schema: bool = True,
) -> FastAPI:
    """Tạo ứng dụng để chạy production hoặc kiểm thử với tài nguyên tạm."""
    active_engine = database_engine or create_database_engine()
    if config.DEMO_MODE:
        configured_storage = any(
            os.environ.get(name)
            for name in (
                "OTTV2_S3_ENDPOINT",
                "OTTV2_S3_BUCKET",
                "OTTV2_S3_ACCESS_KEY_ID",
                "OTTV2_S3_SECRET_ACCESS_KEY",
            )
        )
        if not config.DATABASE_URL.startswith("sqlite:") or active_engine.dialect.name != "sqlite":
            raise RuntimeError("Demo mode requires a local SQLite database")
        if config.REQUIRE_REMOTE_SANDBOX or config.SANDBOX_URL or config.SANDBOX_TOKEN:
            raise RuntimeError("Demo mode cannot use a remote sandbox")
        if configured_storage:
            raise RuntimeError("Demo mode cannot use external object storage")
    object_storage = ObjectStorage.from_environment()
    if config.REQUIRE_REMOTE_SANDBOX:
        if not config.DATABASE_URL.startswith("postgresql+psycopg://"):
            raise RuntimeError("Hosted deployment requires a PostgreSQL database")
        if not object_storage.enabled:
            raise RuntimeError("Hosted deployment requires configured S3-compatible storage")
        if not config.SANDBOX_URL or not config.SANDBOX_TOKEN:
            raise RuntimeError(
                "OTTV2_SANDBOX_URL and OTTV2_SANDBOX_TOKEN are required when remote sandbox is mandatory"
            )
        if not config.SANDBOX_URL.startswith("https://"):
            raise RuntimeError("Hosted remote sandbox URL must use HTTPS")
    elif config.SANDBOX_URL and not config.SANDBOX_TOKEN:
        raise RuntimeError("OTTV2_SANDBOX_TOKEN is required when OTTV2_SANDBOX_URL is set")
    sessions = sessionmaker(bind=active_engine, autoflush=False, expire_on_commit=False)
    submissions_root = Path(submission_directory).resolve()
    replays_root = Path(replay_directory).resolve()
    leaderboards_root = Path(leaderboard_directory).resolve()
    static_root = config.BASE_DIR / "static"
    templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
    timezone = ZoneInfo(config.TIMEZONE_NAME)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Khởi tạo schema khi app bắt đầu và nhường quyền cho máy chủ."""
        del application
        if initialize_schema:
            initialize_database(active_engine)
            bootstrap_id = os.environ.get(config.BOOTSTRAP_ADMIN_ID_ENV, "")
            bootstrap_password = os.environ.get(config.BOOTSTRAP_ADMIN_PASSWORD_ENV, "")
            if bool(bootstrap_id) != bool(bootstrap_password):
                raise RuntimeError("Both bootstrap administrator environment variables are required")
            if config.DEMO_MODE and not bootstrap_id:
                raise RuntimeError("Demo mode requires bootstrap administrator environment variables")
            if bootstrap_id:
                with sessions() as session:
                    try:
                        bootstrap_admin(session, bootstrap_id, bootstrap_password)
                    except IntegrityError:
                        session.rollback()
        yield

    app = FastAPI(title="OTTv2 API", version="1.0.0", lifespan=lifespan)
    app.state.database_engine = active_engine
    app.state.session_factory = sessions
    app.state.clock = lambda: datetime.now(timezone)
    app.state.login_limiter = RateLimiter(
        config.LOGIN_RATE_LIMIT, config.RATE_LIMIT_WINDOW_SECONDS
    )
    app.state.submit_limiter = RateLimiter(
        config.SUBMIT_RATE_LIMIT, config.RATE_LIMIT_WINDOW_SECONDS
    )
    admin_workflows = AdminWorkflows(
        sessions,
        submission_directory,
        replay_directory,
        leaderboard_directory,
        clock=lambda: app.state.clock(),
        object_storage=object_storage,
    )
    app.state.tournament_trigger = tournament_trigger or admin_workflows.start_scoring
    app.state.publish_trigger = publish_trigger or admin_workflows.publish_day
    app.state.object_storage = object_storage
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    def database_session() -> Generator[Session, None, None]:
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    def current_user(
        request: Request,
        session: Session = Depends(database_session),
    ) -> User:
        token = request.cookies.get(config.SESSION_COOKIE_NAME)
        claims = _decode_jwt(token, secret_key) if token else None
        if claims is None:
            raise _api_error(401, "AUTH_REQUIRED", "Vui lòng đăng nhập")
        student_id = claims.get("sub")
        if not isinstance(student_id, str):
            raise _api_error(401, "AUTH_REQUIRED", "Phiên đăng nhập không hợp lệ")
        user = session.get(User, student_id)
        if user is None:
            raise _api_error(401, "AUTH_REQUIRED", "Tài khoản không còn tồn tại")
        return user

    def require_csrf(request: Request) -> None:
        cookie_token = request.cookies.get(config.CSRF_COOKIE_NAME, "")
        header_token = request.headers.get("X-CSRF-Token", "")
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
        ):
            raise _api_error(403, "CSRF_FAILED", "Mã chống CSRF không hợp lệ")

    def get_run_for_today(session: Session, now: datetime) -> TournamentRun | None:
        day_no, round_no = _competition_day(now.date())
        if day_no == 0:
            return None
        return session.scalar(
            select(TournamentRun)
            .where(
                TournamentRun.day_no == day_no,
                TournamentRun.round_no == round_no,
            )
            .order_by(desc(TournamentRun.started_at))
            .limit(1)
        )

    def status_payload(session: Session, now: datetime) -> dict[str, Any]:
        day_no, round_no = _competition_day(now.date())
        run = get_run_for_today(session, now)
        run_status = run.status.upper() if run is not None else ""
        submissions_locked = bool(_setting_value(session, "submissions_locked", False))
        published_day = int(_setting_value(session, "published_day", 0))
        published = published_day == day_no or run_status == "PUBLISHED"
        if run_status in {"RUNNING", "PENDING"}:
            status = "RUNNING"
        elif published:
            status = "PUBLISHED"
        elif submissions_locked:
            status = "LOCKED"
        else:
            status = "OPEN"
        if day_no == 0:
            day_no = 1
            round_no = 1
        return {
            "status": status,
            "day_no": day_no,
            "round_no": round_no,
            "run_partial": bool(run.partial) if run is not None else False,
            "submissions_locked": submissions_locked,
            "published": published,
            "server_time": now.isoformat(),
        }

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        """Chuẩn hóa lỗi HTTP theo một cấu trúc duy nhất."""
        del request
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            payload = {"error": exc.detail}
        else:
            payload = {
                "error": {
                    "code": "HTTP_ERROR",
                    "message": str(exc.detail),
                }
            }
        return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Chuẩn hóa lỗi kiểm tra request, không trả chi tiết nhạy cảm."""
        del request
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Dữ liệu gửi lên không hợp lệ",
                    "details": [
                        {"field": ".".join(map(str, error["loc"])), "message": error["msg"]}
                        for error in exc.errors()
                    ],
                }
            },
            status_code=422,
        )

    @app.post("/api/auth/login")
    def login(
        payload: LoginRequest,
        request: Request,
        response: Response,
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Xác thực student_id và cấp cookie phiên HttpOnly."""
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.login_limiter.allow(f"ip:{client_ip}") or not app.state.login_limiter.allow(
            f"student:{payload.student_id}"
        ):
            raise _api_error(
                429,
                "RATE_LIMITED",
                "Quá nhiều lần đăng nhập, vui lòng thử lại sau",
                headers={"Retry-After": str(config.RATE_LIMIT_WINDOW_SECONDS)},
            )
        if re.fullmatch(config.STUDENT_ID_PATTERN, payload.student_id) is None:
            raise _api_error(401, "INVALID_CREDENTIALS", "Mã thí sinh hoặc mật khẩu không đúng")
        user = session.get(User, payload.student_id)
        verified = False
        if user is not None:
            try:
                verified = password_hasher.verify(user.password_hash, payload.password)
            except (VerifyMismatchError, InvalidHashError, VerificationError):
                verified = False
        if not verified or user is None:
            raise _api_error(401, "INVALID_CREDENTIALS", "Mã thí sinh hoặc mật khẩu không đúng")

        now = int(time.time())
        token = _encode_jwt(
            {"sub": user.student_id, "iat": now, "exp": now + config.JWT_TTL_SECONDS},
            secret_key,
        )
        csrf_token = secrets.token_urlsafe(32)
        response.set_cookie(
            config.SESSION_COOKIE_NAME,
            token,
            httponly=True,
            secure=config.COOKIE_SECURE,
            samesite="lax",
            max_age=config.JWT_TTL_SECONDS,
            path="/",
        )
        response.set_cookie(
            config.CSRF_COOKIE_NAME,
            csrf_token,
            httponly=False,
            secure=config.COOKIE_SECURE,
            samesite="lax",
            max_age=config.JWT_TTL_SECONDS,
            path="/",
        )
        return {
            "student_id": user.student_id,
            "is_admin": user.is_admin,
            "csrf_token": csrf_token,
        }

    @app.get("/", include_in_schema=False)
    def index_page(request: Request) -> Response:
        """Phục vụ trang bảng xếp hạng."""
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"page": "dashboard", "template_name": "dashboard.html"},
        )

    @app.get("/submit", include_in_schema=False)
    def submission_page(request: Request) -> Response:
        """Phục vụ trang nộp bot."""
        return templates.TemplateResponse(
            request=request,
            name="submit.html",
            context={"page": "submit", "template_name": "submit.html"},
        )

    @app.get("/profile", include_in_schema=False)
    def profile_page(request: Request) -> Response:
        """Phục vụ trang hồ sơ và replay."""
        return templates.TemplateResponse(
            request=request,
            name="profile.html",
            context={"page": "profile", "template_name": "profile.html"},
        )

    @app.get("/admin", include_in_schema=False)
    def admin_page(request: Request) -> Response:
        """Phục vụ trang quản trị; các thao tác vẫn được bảo vệ bởi API."""
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={"page": "admin", "template_name": "admin.html"},
        )

    @app.post("/api/auth/logout")
    def logout(request: Request) -> JSONResponse:
        """Xóa cookie phiên và CSRF sau khi kiểm tra mã chống giả mạo."""
        require_csrf(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
        response.delete_cookie(config.CSRF_COOKIE_NAME, path="/")
        return response

    @app.post("/admin/accounts")
    def admin_invite_participant(
        payload: InviteParticipantRequest,
        request: Request,
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, str]:
        """Create a participant account using a password chosen by the administrator."""
        require_csrf(request)
        if not user.is_admin:
            raise _api_error(403, "ADMIN_REQUIRED", "Bạn không có quyền quản trị")
        if re.fullmatch(config.STUDENT_ID_PATTERN, payload.student_id) is None:
            raise _api_error(422, "INVALID_STUDENT_ID", "Mã thí sinh không hợp lệ")
        if session.get(User, payload.student_id) is not None:
            raise _api_error(409, "ACCOUNT_ALREADY_EXISTS", "Mã thí sinh đã được sử dụng")
        try:
            create_user(session, payload.student_id, payload.password)
        except IntegrityError:
            session.rollback()
            raise _api_error(409, "ACCOUNT_ALREADY_EXISTS", "Mã thí sinh đã được sử dụng")
        logger.info(
            "participant_account_created",
            extra={"admin_id": user.student_id, "student_id": payload.student_id},
        )
        return {"student_id": payload.student_id}

    @app.post("/api/submit")
    def submit(
        request: Request,
        file: UploadFile = File(...),
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Kiểm tra mã nguồn và ghi phiên bản bot riêng tư theo atomic replace."""
        require_csrf(request)
        if user.is_admin:
            raise _api_error(403, "ADMIN_CANNOT_SUBMIT", "Tài khoản quản trị không thể nộp bot")
        if not app.state.submit_limiter.allow(f"student:{user.student_id}"):
            raise _api_error(
                429,
                "RATE_LIMITED",
                "Quá nhiều lần nộp bài, vui lòng thử lại sau",
                headers={"Retry-After": str(config.RATE_LIMIT_WINDOW_SECONDS)},
            )
        now = app.state.clock().astimezone(timezone)
        if status_payload(session, now)["status"] in {"LOCKED", "RUNNING"}:
            raise _api_error(403, "SUBMISSION_LOCKED", "Đã khóa nộp bài")
        if Path(file.filename or "").suffix.lower() != ".py":
            raise _api_error(422, "INVALID_EXTENSION", "Chỉ chấp nhận tệp có đuôi .py")
        raw_source = file.file.read(config.BOT_FILE_SIZE_BYTES + 1)
        if len(raw_source) > config.BOT_FILE_SIZE_BYTES:
            raise _api_error(413, "FILE_TOO_LARGE", "Tệp bot vượt quá giới hạn 100 KiB")
        try:
            source = raw_source.decode("utf-8")
        except UnicodeDecodeError:
            raise _api_error(422, "INVALID_ENCODING", "Tệp bot phải được mã hóa UTF-8")
        scan = scan_bot_source(source)
        if not scan.valid:
            raise _api_error(
                422,
                "BOT_VALIDATION_FAILED",
                "Tệp bot không hợp lệ",
                details=list(scan.errors),
            )

        student_id = user.student_id
        if re.fullmatch(config.STUDENT_ID_PATTERN, student_id) is None:
            raise _api_error(400, "INVALID_STUDENT_ID", "Mã thí sinh không hợp lệ")
        latest_version = session.scalar(
            select(func.max(Submission.version)).where(Submission.student_id == student_id)
        ) or 0
        version = latest_version + 1
        student_directory = (submissions_root / student_id).resolve()
        if student_directory.parent != submissions_root:
            raise _api_error(400, "INVALID_STUDENT_ID", "Mã thí sinh không hợp lệ")
        submissions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(submissions_root, 0o700)
        student_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(student_directory, 0o700)
        version_path = student_directory / f"version_{version}.py"
        current_path = submissions_root / f"{student_id}.py"
        digest = hashlib.sha256(raw_source).hexdigest()
        _write_private_atomic(version_path, raw_source)
        object_storage.put_path(version_path, submissions_root, "text/x-python")
        _write_private_atomic(current_path, raw_source)
        object_storage.put_path(current_path, submissions_root, "text/x-python")

        submission = Submission(
            student_id=student_id,
            version=version,
            sha256=digest,
            file_path=str(version_path),
            valid=True,
            submitted_at=now,
        )
        session.add(submission)
        session.commit()
        logger.info(
            "submission_accepted",
            extra={"student_id": student_id, "version": version, "sha256": digest},
        )
        return {
            "ok": True,
            "version": version,
            "sha256": digest,
            "submitted_at": now.isoformat(),
            "checks": {
                "extension": True,
                "size": True,
                "utf8": True,
                "ast": True,
                "make_move": True,
                "sandbox_smoke_test": True,
            },
        }

    @app.get("/api/my/submission")
    def my_submission(
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Trả metadata và URL tải bài mới nhất của chính người đăng nhập."""
        submission = _latest_submission(session, user.student_id)
        if submission is None:
            raise _api_error(404, "SUBMISSION_NOT_FOUND", "Bạn chưa có bài nộp hợp lệ")
        return {
            "student_id": user.student_id,
            "version": submission.version,
            "sha256": submission.sha256,
            "submitted_at": _iso_datetime(submission.submitted_at, timezone),
            "download_url": "/api/my/submission/file",
        }

    @app.get("/api/my/submission/file")
    def download_my_submission(
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> Response:
        """Tải bài mới nhất của tài khoản hiện tại, không nhận ID từ query."""
        submission = _latest_submission(session, user.student_id)
        if submission is None:
            raise _api_error(404, "SUBMISSION_NOT_FOUND", "Bạn chưa có bài nộp hợp lệ")
        path = _safe_submission_path(submissions_root, submission.file_path)
        try:
            source = object_storage.read_path(path, submissions_root)
        except FileNotFoundError:
            raise _api_error(404, "SUBMISSION_NOT_FOUND", "Không tìm thấy tệp bài nộp")
        return Response(
            content=source,
            media_type="text/x-python",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{user.student_id}.py"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/my/results")
    def my_results(
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Trả lịch sử rank ngày riêng của tài khoản hiện tại."""
        rows = session.scalars(
            select(DailyRank)
            .where(DailyRank.student_id == user.student_id)
            .order_by(DailyRank.day_no)
        ).all()
        return {
            "student_id": user.student_id,
            "is_admin": user.is_admin,
            "results": [
                {
                    "tournament_id": row.tournament_id,
                    "day_no": row.day_no,
                    "rank": row.rank,
                    "points": row.points,
                    "top32": row.top32,
                    "absent": row.absent,
                }
                for row in rows
            ],
        }

    @app.get("/api/my/replays")
    def my_replays(
        request: Request,
        day: int | None = None,
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Liệt kê replay đã cấp cho người dùng mà không đính kèm nước đi."""
        query = (
            select(ReplayAccess, Game)
            .join(Game, ReplayAccess.game_id == Game.game_id)
            .where(
                ReplayAccess.student_id == user.student_id,
                (Game.p1_id == user.student_id) | (Game.p2_id == user.student_id),
            )
            .order_by(ReplayAccess.order_no)
        )
        if day is not None:
            query = query.where(Game.day_no == day)
        rows = session.execute(query).all()
        current_day, _ = _competition_day(app.state.clock().astimezone(timezone).date())
        selected_day = day if day is not None else current_day
        games_query = select(func.count(Game.game_id)).where(
            (Game.p1_id == user.student_id) | (Game.p2_id == user.student_id)
        )
        if selected_day > 0:
            games_query = games_query.where(Game.day_no == selected_day)
        n_games = int(session.scalar(games_query) or 0)
        return {
            "student_id": user.student_id,
            "quota": replay_quota(n_games),
            "n_games": n_games,
            "selected": len(rows),
            "replays": [
                _replay_list_item(user.student_id, game, replay_access.order_no, session)
                for replay_access, game in rows
            ],
        }

    @app.get("/api/replay/{game_id}")
    def get_replay(
        game_id: str,
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Chỉ trả frame nếu người gọi là người chơi và có quota replay."""
        access = session.scalar(
            select(ReplayAccess).where(
                ReplayAccess.student_id == user.student_id,
                ReplayAccess.game_id == game_id,
            )
        )
        game = session.get(Game, game_id)
        if (
            access is None
            or game is None
            or user.student_id not in (game.p1_id, game.p2_id)
        ):
            raise _api_error(403, "REPLAY_FORBIDDEN", "Bạn không được phép xem ván đấu này")
        replay_path = _safe_replay_path(replays_root, game.replay_file)
        try:
            object_storage.ensure_local(replay_path, replays_root)
            moves_bytes = ReplayStore(replay_path).read(game.replay_offset, game.replay_length)
            frames = build_replay_frames(moves_bytes)
        except (OSError, ReplayFormatError) as exc:
            logger.exception("replay_read_failed", extra={"game_id": game_id})
            raise _api_error(500, "REPLAY_UNAVAILABLE", "Không thể dựng replay") from exc
        return {
            "game_id": game.game_id,
            "result": game.result,
            "win_reason": game.win_reason,
            "plies": game.plies,
            "captured_p1": game.captured_p1,
            "captured_p2": game.captured_p2,
            "frames": [_frame_payload(frame) for frame in frames],
        }

    @app.get("/api/status")
    def api_status(
        request: Request,
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Trả lịch giải và trạng thái nộp theo đồng hồ server."""
        return status_payload(session, request.app.state.clock().astimezone(timezone))

    @app.get("/api/leaderboard/{kind}")
    def get_leaderboard(
        kind: str,
        request: Request,
        round_no: int | None = None,
    ) -> Response:
        """Phục vụ file JSON tĩnh, không truy vấn database."""
        if kind == "daily":
            filename = "daily.json"
        elif kind == "tournament":
            filename = "tournament.json"
        elif kind == "round" and round_no is not None and 1 <= round_no <= 5:
            filename = f"round_{round_no}.json"
        else:
            raise _api_error(404, "LEADERBOARD_NOT_FOUND", "Không tìm thấy bảng xếp hạng")
        path = (leaderboards_root / filename).resolve()
        if path.parent != leaderboards_root:
            raise _api_error(404, "LEADERBOARD_NOT_FOUND", "Bảng xếp hạng chưa được công bố")
        try:
            contents = object_storage.read_path(path, leaderboards_root)
        except FileNotFoundError:
            raise _api_error(404, "LEADERBOARD_NOT_FOUND", "Bảng xếp hạng chưa được công bố")
        etag = f'"{hashlib.sha256(contents).hexdigest()}"'
        headers = {"Cache-Control": "public, max-age=60, must-revalidate", "ETag": etag}
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(contents, media_type="application/json", headers=headers)

    @app.post("/admin/run")
    @app.post("/admin/score")
    def admin_run(
        request: Request,
        day: int,
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Khởi chạy lượt chấm nền sau khi admin đã khóa nhận bài."""
        require_csrf(request)
        if not user.is_admin:
            raise _api_error(403, "ADMIN_REQUIRED", "Bạn không có quyền quản trị")
        if not 1 <= day <= config.TOURNAMENT_DAYS:
            raise _api_error(422, "INVALID_DAY", "day phải nằm trong 1..35")
        if not bool(_setting_value(session, "submissions_locked", False)):
            raise _api_error(
                409,
                "SUBMISSIONS_MUST_BE_LOCKED",
                "Hãy khóa nhận bài trước khi bắt đầu chấm",
            )
        active_run = session.scalar(
            select(TournamentRun).where(
                TournamentRun.day_no == day,
                TournamentRun.status == "RUNNING",
            )
        )
        if active_run is not None:
            raise _api_error(409, "RUN_ALREADY_ACTIVE", "Ngày này đang được chấm")
        trigger = app.state.tournament_trigger
        if trigger is None:
            raise _api_error(503, "RUNNER_UNAVAILABLE", "Chưa cấu hình bộ kích hoạt chấm giải")
        try:
            run_id = trigger(day)
        except RuntimeError as exc:
            raise _api_error(409, "RUN_NOT_STARTED", str(exc)) from exc
        return {"run_id": run_id, "day_no": day, "status": "QUEUED"}

    @app.get("/admin/control")
    def admin_control(
        request: Request,
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Trả trạng thái vận hành cho dashboard quản trị."""
        if not user.is_admin:
            raise _api_error(403, "ADMIN_REQUIRED", "Bạn không có quyền quản trị")
        now = request.app.state.clock().astimezone(timezone)
        day_no, round_no = _competition_day(now.date())
        day_no = day_no or 1
        run = get_run_for_today(session, now)
        return {
            "day_no": day_no,
            "round_no": round_no or 1,
            "locked": bool(_setting_value(session, "submissions_locked", False)),
            "lock_reason": _setting_value(session, "lock_reason", ""),
            "status": run.status if run is not None else "IDLE",
            "partial": bool(run.partial) if run is not None else False,
            "run_id": run.run_id if run is not None else None,
            "checkpoint_stage": run.checkpoint_stage if run is not None else None,
            "published": int(_setting_value(session, "published_day", 0)) == day_no,
            "score_available": app.state.tournament_trigger is not None,
            "publish_available": app.state.publish_trigger is not None,
        }

    @app.post("/admin/submissions/lock")
    def admin_submission_lock(
        payload: SubmissionLockRequest,
        request: Request,
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Khóa hoặc mở nhận bài thủ công, có hiệu lực ngay trên mọi giờ."""
        require_csrf(request)
        if not user.is_admin:
            raise _api_error(403, "ADMIN_REQUIRED", "Bạn không có quyền quản trị")
        now = request.app.state.clock().astimezone(timezone)
        session.merge(
            SystemSetting(
                key="submissions_locked",
                value=payload.locked,
                updated_at=now,
            )
        )
        session.merge(
            SystemSetting(
                key="lock_reason",
                value="Khóa bởi quản trị viên" if payload.locked else "",
                updated_at=now,
            )
        )
        session.commit()
        logger.info(
            "submission_lock_changed",
            extra={"admin_id": user.student_id, "locked": payload.locked},
        )
        return {"locked": payload.locked, "updated_at": now.isoformat()}

    @app.post("/admin/publish")
    def admin_publish(
        request: Request,
        day: int,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Xuất leaderboard sau khi lượt chấm hoàn tất."""
        require_csrf(request)
        if not user.is_admin:
            raise _api_error(403, "ADMIN_REQUIRED", "Bạn không có quyền quản trị")
        if not 1 <= day <= config.TOURNAMENT_DAYS:
            raise _api_error(422, "INVALID_DAY", "day phải nằm trong 1..35")
        trigger = app.state.publish_trigger
        if trigger is None:
            raise _api_error(503, "PUBLISHER_UNAVAILABLE", "Chưa cấu hình bộ công bố bảng xếp hạng")
        try:
            result = trigger(day)
        except RuntimeError as exc:
            raise _api_error(409, "PUBLISH_NOT_READY", str(exc)) from exc
        logger.info(
            "leaderboard_published",
            extra={"admin_id": user.student_id, "day_no": day},
        )
        return {"status": "PUBLISHED", **result}

    @app.get("/admin/runs")
    def admin_runs(
        user: User = Depends(current_user),
        session: Session = Depends(database_session),
    ) -> dict[str, Any]:
        """Liệt kê metadata các lượt chấm chỉ cho quản trị viên."""
        if not user.is_admin:
            raise _api_error(403, "ADMIN_REQUIRED", "Bạn không có quyền quản trị")
        rows = session.scalars(
            select(TournamentRun).order_by(desc(TournamentRun.started_at)).limit(100)
        ).all()
        return {
            "runs": [
                {
                    "run_id": run.run_id,
                    "tournament_id": run.tournament_id,
                    "round_no": run.round_no,
                    "day_no": run.day_no,
                    "status": run.status,
                    "partial": run.partial,
                    "checkpoint_stage": run.checkpoint_stage,
                    "started_at": _iso_datetime(run.started_at, timezone),
                    "completed_at": _iso_datetime(run.completed_at, timezone),
                }
                for run in rows
            ]
        }

    @app.get("/healthz")
    def healthz(session: Session = Depends(database_session)) -> dict[str, str]:
        """Kiểm tra khả năng truy cập database."""
        session.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


def _competition_day(today: date) -> tuple[int, int]:
    """Tính ngày 1..35 và vòng 1..5 từ ngày bắt đầu cấu hình."""
    delta = (today - config.TOURNAMENT_START_DATE).days
    if delta < 0:
        return 0, 0
    day_no = min(delta + 1, config.TOURNAMENT_DAYS)
    round_no = min((day_no - 1) // config.ROUND_DAYS + 1, 5)
    return day_no, round_no


def _setting_value(session: Session, key: str, default: Any) -> Any:
    """Đọc một thiết lập điều hành hoặc trả về mặc định an toàn."""
    setting = session.get(SystemSetting, key)
    return setting.value if setting is not None else default


def _encode_jwt(claims: dict[str, Any], secret_key: str) -> str:
    """Tạo JWT HS256 với JSON canonical và base64url."""
    header = {"alg": "HS256", "typ": "JWT"}
    parts = (_base64url(json.dumps(value, separators=(",", ":")).encode("utf-8")) for value in (header, claims))
    signing_input = ".".join(parts)
    signature = hmac.new(secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_base64url(signature)}"


def _decode_jwt(token: str, secret_key: str) -> dict[str, Any] | None:
    """Xác minh chữ ký HS256 và thời hạn JWT."""
    try:
        header_part, claims_part, signature_part = token.split(".")
        signing_input = f"{header_part}.{claims_part}"
        expected = hmac.new(
            secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _base64url_decode(signature_part)):
            return None
        header = json.loads(_base64url_decode(header_part))
        claims = json.loads(_base64url_decode(claims_part))
        if header.get("alg") != "HS256" or not isinstance(claims, dict):
            return None
        if not isinstance(claims.get("exp"), int) or claims["exp"] <= int(time.time()):
            return None
        return claims
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _base64url(value: bytes) -> str:
    """Mã hóa base64url không có dấu đệm."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    """Giải mã base64url có bổ sung padding."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _api_error(
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """Tạo ngoại lệ API theo envelope lỗi thống nhất."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return HTTPException(status_code, detail=error, headers=headers)


def _write_private_atomic(path: Path, content: bytes) -> None:
    """Ghi tệp riêng tư bằng rename atomic trong cùng thư mục."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _latest_submission(session: Session, student_id: str) -> Submission | None:
    """Lấy phiên bản hợp lệ mới nhất của đúng một tài khoản."""
    return session.scalar(
        select(Submission)
        .where(Submission.student_id == student_id, Submission.valid.is_(True))
        .order_by(desc(Submission.version))
        .limit(1)
    )


def _safe_submission_path(root: Path, stored_path: str) -> Path:
    """Đảm bảo đường dẫn từ DB vẫn nằm trong kho submission riêng."""
    path = Path(stored_path).resolve()
    if root not in path.parents:
        raise _api_error(404, "SUBMISSION_NOT_FOUND", "Không tìm thấy tệp bài nộp")
    return path


def _safe_replay_path(root: Path, filename: str) -> Path:
    """Chỉ cho phép basename replay, không chấp nhận đường dẫn trong DB."""
    if Path(filename).name != filename or not filename.endswith(".bin"):
        raise _api_error(404, "REPLAY_NOT_FOUND", "Không tìm thấy replay")
    path = (root / filename).resolve()
    if path.parent != root:
        raise _api_error(404, "REPLAY_NOT_FOUND", "Không tìm thấy replay")
    return path


def _replay_list_item(
    student_id: str,
    game: Game,
    order_no: int,
    session: Session,
) -> dict[str, Any]:
    """Tạo dòng replay không chứa nước đi và ẩn danh đối thủ."""
    opponent_id = game.p2_id if game.p1_id == student_id else game.p1_id
    opponent = opponent_id
    if not config.REPLAY_SHOW_OPPONENT_ID:
        rank = session.scalar(
            select(DailyRank.rank)
            .where(DailyRank.student_id == opponent_id, DailyRank.day_no == game.day_no)
            .order_by(desc(DailyRank.tournament_id))
            .limit(1)
        )
        opponent = f"Đối thủ hạng #{rank}" if rank is not None else "Đối thủ"
    if game.result == "DRAW":
        outcome = "Hòa"
    elif game.result == ("P1" if game.p1_id == student_id else "P2"):
        outcome = "Thắng"
    else:
        outcome = "Thua"
    stage_label = (
        "GĐ1 – Benchmark"
        if game.stage == 1
        else f"GĐ2 – Vòng {game.round_in_stage}"
        if game.stage == 2
        else f"GĐ3 – Vòng {game.round_in_stage}"
    )
    return {
        "game_id": game.game_id,
        "order_no": order_no,
        "day_no": game.day_no,
        "stage": stage_label,
        "opponent": opponent,
        "result": outcome,
        "win_reason": game.win_reason,
        "plies": game.plies,
    }


def _frame_payload(frame: Any) -> dict[str, Any]:
    """Chuyển ReplayFrame thành JSON-compatible dữ liệu."""
    return {
        "ply": frame.ply,
        "board": frame.board,
        "move": frame.move,
        "captured_piece": frame.captured_piece,
        "remaining_p1": frame.remaining_p1,
        "remaining_p2": frame.remaining_p2,
    }


def _iso_datetime(value: datetime | None, timezone: ZoneInfo) -> str | None:
    """Định dạng datetime, gắn timezone cấu hình cho giá trị naive từ SQLite."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(timezone).isoformat()


app = create_app()
