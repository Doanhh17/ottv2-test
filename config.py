"""Cấu hình luật và các giả định khởi tạo của OTTv2."""

import os
import secrets
from pathlib import Path
from datetime import date
from typing import Final


# Các giả định của giai đoạn A:
# - Phe 1 đi trước; mỗi phe được mã hóa bằng số 1 hoặc 2.
# - Bàn cờ khởi đầu có tam giác 9 ô (r + c <= 3), bỏ trống ô góc nhà.
# - Ba loại quân được phân bố theo thứ tự trong INITIAL_LAYOUT_P1, mỗi loại 3 quân.
# - Hết nước đi hợp lệ khi đến lượt thì phe đó thua.
# - Nếu trạng thái khởi tạo tùy chỉnh đã thỏa điều kiện thắng của cả hai phe,
#   kết quả là hòa; nếu chỉ một phe thỏa thì phe đó thắng.
# - Kết quả và lý do chỉ được truy vấn sau khi ván đã kết thúc.

BOARD_SIZE: Final[int] = 9
PLAYERS: Final[tuple[int, int]] = (1, 2)
INITIAL_PLAYER: Final[int] = 1
MAX_PLIES: Final[int] = 150
PIECE_TYPES: Final[tuple[str, str, str]] = ("R", "S", "P")
PIECE_BEATS: Final[dict[str, str]] = {"R": "S", "S": "P", "P": "R"}
MOVE_OFFSETS: Final[tuple[tuple[int, int], ...]] = tuple(
    (row_delta, col_delta)
    for row_delta in (-1, 0, 1)
    for col_delta in (-1, 0, 1)
    if row_delta != 0 or col_delta != 0
)
GOAL_BY_PLAYER: Final[dict[int, tuple[int, int]]] = {1: (8, 8), 2: (0, 0)}
INITIAL_LAYOUT_P1: Final[tuple[tuple[tuple[int, int], str], ...]] = (
    ((0, 1), "R"),
    ((0, 2), "S"),
    ((0, 3), "P"),
    ((1, 0), "S"),
    ((1, 1), "P"),
    ((1, 2), "R"),
    ((2, 0), "P"),
    ((2, 1), "R"),
    ((3, 0), "S"),
)

BOT_MOVE_TIMEOUT_SECONDS: Final[float] = 0.2
BOT_GAME_TIMEOUT_SECONDS: Final[float] = 10.0
BOT_MEMORY_LIMIT_BYTES: Final[int] = 256 * 1024 * 1024
BOT_FILE_SIZE_BYTES: Final[int] = 100 * 1024
BOT_MAX_OPEN_FILES: Final[int] = 16
BOT_MAX_PROCESSES: Final[int] = 1
BOT_CPU_SOFT_LIMIT_SECONDS: Final[int] = 1
BOT_CPU_HARD_LIMIT_SECONDS: Final[int] = 2
BOT_MAX_AST_NODES: Final[int] = 20_000
BOT_RANDOM_SEED: Final[int] = 20_260_415
BOT_ALLOWED_IMPORTS: Final[frozenset[str]] = frozenset(
    {"random", "math", "itertools", "collections", "heapq", "functools", "copy"}
)
BOT_FORBIDDEN_NAMES: Final[frozenset[str]] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "ctypes",
        "importlib",
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "__import__",
    }
)

TOURNAMENT_WORKERS: Final[int] = max(1, os.cpu_count() or 1)
GATEKEEPER_GAMES_PER_BOT: Final[int] = 6
GATEKEEPER_CUTOFF: Final[int] = 1024
SWISS_ROUNDS: Final[int] = 10
SWISS_GAMES_PER_MATCH: Final[int] = 2
PLAYOFF_CUTOFF: Final[int] = 32
DOUBLE_ELIMINATION_RESET: Final[bool] = False
TOURNAMENT_ID: Final[str] = os.environ.get("OTTV2_TOURNAMENT_ID", "ottv2-2026")
ADMIN_SCORE_WORKERS: Final[int] = min(4, max(1, os.cpu_count() or 1))

BASE_DIR: Final[Path] = Path(__file__).resolve().parent
STORAGE_DIR: Final[Path] = BASE_DIR / "storage"
REPLAY_STORAGE_DIR: Final[Path] = STORAGE_DIR / "replays"
LEADERBOARD_STORAGE_DIR: Final[Path] = BASE_DIR / "static" / "leaderboards"
DATABASE_URL: Final[str] = os.environ.get(
    "OTTV2_DATABASE_URL", f"sqlite:///{(STORAGE_DIR / 'ottv2.db').as_posix()}"
)
SANDBOX_URL: Final[str] = os.environ.get("OTTV2_SANDBOX_URL", "").rstrip("/")
SANDBOX_TOKEN: Final[str] = os.environ.get("OTTV2_SANDBOX_TOKEN", "")
REQUIRE_REMOTE_SANDBOX: Final[bool] = (
    os.environ.get("OTTV2_REQUIRE_REMOTE_SANDBOX", "0") == "1"
)
TIMEZONE_NAME: Final[str] = "Asia/Ho_Chi_Minh"
TOURNAMENT_START_HOUR: Final[int] = 1
TOURNAMENT_END_HOUR: Final[int] = 5
LEADERBOARD_PUBLISH_HOUR: Final[int] = 6
REPLAY_QUOTA_RATIO: Final[float] = 0.25
REPLAY_QUOTA_MAX: Final[int] = 5
REPLAY_PICK_MODE: Final[str] = "auto"
REPLAY_RETENTION_DAYS: Final[int] = 7
PUBLIC_TOP32_REPLAYS: Final[bool] = False
ABSENT_DAILY_RANK: Final[int] = 4000
SUBMISSION_STORAGE_DIR: Final[Path] = STORAGE_DIR / "submissions"
STUDENT_ID_PATTERN: Final[str] = r"^[A-Za-z0-9_-]{3,32}$"
JWT_SECRET_KEY: Final[str] = os.environ.get("OTTV2_SECRET_KEY") or secrets.token_urlsafe(48)
JWT_TTL_SECONDS: Final[int] = 12 * 60 * 60
SESSION_COOKIE_NAME: Final[str] = "ottv2_session"
CSRF_COOKIE_NAME: Final[str] = "ottv2_csrf"
COOKIE_SECURE: Final[bool] = os.environ.get("OTTV2_COOKIE_SECURE", "0") == "1"
LOGIN_RATE_LIMIT: Final[int] = 5
SUBMIT_RATE_LIMIT: Final[int] = 5
RATE_LIMIT_WINDOW_SECONDS: Final[int] = 60
TOURNAMENT_START_DATE: Final[date] = date.fromisoformat(
    os.environ.get("OTTV2_TOURNAMENT_START_DATE", "2026-10-15")
)
TOURNAMENT_DAYS: Final[int] = 35
ROUND_DAYS: Final[int] = 7
REPLAY_SHOW_OPPONENT_ID: Final[bool] = False
