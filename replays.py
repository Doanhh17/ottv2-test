"""Lưu replay nối tiếp, chọn quota và dựng frame bằng luật engine."""

import math
import os
import struct
import tempfile
import threading
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence

import config
from engine import Move, OTTGame, Piece


@dataclass(frozen=True, slots=True)
class ReplayCandidate:
    """Metadata cần để chọn replay cho một người chơi."""

    game_id: str
    p1_id: str
    p2_id: str
    stage: int
    round_in_stage: int
    opponent_strength: float
    plies: int
    result: str
    win_reason: str

    def stage_depth(self) -> int:
        """Tính độ sâu ưu tiên theo giai đoạn và vòng nhánh."""
        if self.stage == 1:
            return 100
        if self.stage == 2:
            return 200 + self.round_in_stage
        if self.stage == 3:
            return 300 + self.round_in_stage
        raise ValueError("stage phải là 1, 2 hoặc 3")


@dataclass(frozen=True, slots=True)
class ReplayAccessChoice:
    """Một quyền xem replay được cấp theo thứ tự ưu tiên."""

    student_id: str
    game_id: str
    order_no: int


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    """Một khung bàn cờ dựng sẵn ở đầu hoặc sau mỗi ply."""

    ply: int
    board: tuple[tuple[Piece | None, ...], ...]
    move: Move | None
    captured_piece: Piece | None
    remaining_p1: int
    remaining_p2: int


class ReplayFormatError(ValueError):
    """Replay bị sai định dạng, bị cắt cụt hoặc chứa nước bất hợp lệ."""


class ReplayStore:
    """File replay append-only, mỗi record nén riêng bằng zlib."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self._lock = threading.Lock()

    def append(self, moves_bytes: bytes) -> tuple[int, int]:
        """Nối record gồm độ dài 4 byte và payload zlib; trả offset/length."""
        validate_moves_bytes(moves_bytes)
        compressed = zlib.compress(moves_bytes, level=6)
        record = struct.pack(">I", len(compressed)) + compressed
        with self._lock:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "ab") as replay_file:
                replay_file.seek(0, os.SEEK_END)
                offset = replay_file.tell()
                replay_file.write(record)
                replay_file.flush()
                os.fsync(replay_file.fileno())
        return offset, len(record)

    def read(self, offset: int, length: int) -> bytes:
        """Đọc, giải nén và xác thực một record theo index."""
        if offset < 0 or length < 5:
            raise ReplayFormatError("Vị trí hoặc độ dài replay không hợp lệ")
        with self.path.open("rb") as replay_file:
            replay_file.seek(offset)
            record = replay_file.read(length)
        if len(record) != length:
            raise ReplayFormatError("Replay bị cắt cụt")
        compressed_length = struct.unpack(">I", record[:4])[0]
        if compressed_length != length - 4:
            raise ReplayFormatError("Độ dài record không khớp")
        try:
            decompressor = zlib.decompressobj()
            moves_bytes = decompressor.decompress(record[4:], config.MAX_PLIES * 2 + 1)
        except zlib.error as exc:
            raise ReplayFormatError("Không giải nén được replay") from exc
        if (
            len(moves_bytes) > config.MAX_PLIES * 2
            or decompressor.unconsumed_tail
            or decompressor.unused_data
            or not decompressor.eof
        ):
            raise ReplayFormatError("Nội dung replay nén không hợp lệ hoặc quá lớn")
        validate_moves_bytes(moves_bytes)
        return moves_bytes


def validate_moves_bytes(moves_bytes: bytes) -> None:
    """Kiểm tra replay dùng hai byte cho mỗi ply và chỉ số ô 0..80."""
    if not isinstance(moves_bytes, bytes) or len(moves_bytes) % 2:
        raise ReplayFormatError("Dữ liệu nước đi phải là bytes có độ dài chẵn")
    if len(moves_bytes) > config.MAX_PLIES * 2:
        raise ReplayFormatError("Replay vượt quá số nước đi tối đa")
    if any(square >= config.BOARD_SIZE * config.BOARD_SIZE for square in moves_bytes):
        raise ReplayFormatError("Chỉ số ô trong replay phải nằm trong 0..80")


def replay_quota(
    n_games: int,
    ratio: float = config.REPLAY_QUOTA_RATIO,
    maximum: int = config.REPLAY_QUOTA_MAX,
) -> int:
    """Tính K = clamp(ceil(ratio * số ván), 1, maximum)."""
    if n_games < 0 or ratio < 0 or maximum < 1:
        raise ValueError("Tham số quota không hợp lệ")
    return min(max(math.ceil(ratio * n_games), 1), maximum)


def choose_replays(
    student_id: str,
    candidates: Iterable[ReplayCandidate],
    mode: Literal["auto", "user"] = config.REPLAY_PICK_MODE,
    selected_game_ids: Sequence[str] = (),
    ratio: float = config.REPLAY_QUOTA_RATIO,
    maximum: int = config.REPLAY_QUOTA_MAX,
) -> tuple[ReplayAccessChoice, ...]:
    """Chọn tối đa K game, tùy chọn ưu tiên lựa chọn người dùng trước."""
    if mode not in ("auto", "user"):
        raise ValueError("mode phải là auto hoặc user")
    player_games = {
        candidate.game_id: candidate
        for candidate in candidates
        if student_id in (candidate.p1_id, candidate.p2_id)
    }
    quota = replay_quota(len(player_games), ratio, maximum)
    chosen: list[ReplayCandidate] = []
    if mode == "user":
        for game_id in selected_game_ids:
            candidate = player_games.get(game_id)
            if candidate is not None and candidate not in chosen:
                chosen.append(candidate)
            if len(chosen) == quota:
                break
    stage_best: dict[int, ReplayCandidate] = {}
    for candidate in sorted(
        player_games.values(),
        key=lambda item: (
            -item.stage_depth(),
            -item.plies,
            -item.opponent_strength,
            item.game_id,
        ),
    ):
        stage_best.setdefault(candidate.stage, candidate)
    chosen.extend(
        candidate
        for candidate in stage_best.values()
        if candidate not in chosen
    )
    chosen = chosen[:quota]
    remaining = sorted(
        (candidate for candidate in player_games.values() if candidate not in chosen),
        key=lambda candidate: (
            -candidate.plies,
            -candidate.stage_depth(),
            -candidate.opponent_strength,
            candidate.game_id,
        ),
    )
    chosen.extend(remaining[: max(0, quota - len(chosen))])
    return tuple(
        ReplayAccessChoice(student_id, candidate.game_id, order_no)
        for order_no, candidate in enumerate(chosen, start=1)
    )


def choose_daily_replay_access(
    candidates: Iterable[ReplayCandidate],
    student_ids: Iterable[str],
    mode: Literal["auto", "user"] = config.REPLAY_PICK_MODE,
    user_selections: Mapping[str, Sequence[str]] | None = None,
) -> tuple[ReplayAccessChoice, ...]:
    """Chọn replay theo quota riêng cho từng người chơi trong ngày."""
    candidate_list = tuple(candidates)
    selections = user_selections or {}
    choices = [
        choice
        for student_id in sorted(set(student_ids))
        for choice in choose_replays(
            student_id,
            candidate_list,
            mode,
            selections.get(student_id, ()),
        )
    ]
    return tuple(choices)


def build_replay_frames(
    moves_bytes: bytes,
    max_plies: int = config.MAX_PLIES,
) -> tuple[ReplayFrame, ...]:
    """Dựng trạng thái đầu và từng ply, xác thực mỗi nước bằng OTTGame."""
    validate_moves_bytes(moves_bytes)
    game = OTTGame(max_plies=max_plies)
    frames = [_frame(game, 0, None, None)]
    for offset in range(0, len(moves_bytes), 2):
        source_index, target_index = moves_bytes[offset : offset + 2]
        move: Move = (
            divmod(source_index, config.BOARD_SIZE),
            divmod(target_index, config.BOARD_SIZE),
        )
        if game.is_over() or move not in game.get_valid_moves(game.current_player):
            raise ReplayFormatError(f"Nước replay thứ {offset // 2 + 1} không hợp lệ")
        board_before = game.to_state().board
        target_row, target_col = move[1]
        captured_piece = board_before[target_row][target_col]
        game.apply_move(move)
        frames.append(_frame(game, game.plies, move, captured_piece))
    return tuple(frames)


def _frame(
    game: OTTGame,
    ply: int,
    move: Move | None,
    captured_piece: Piece | None,
) -> ReplayFrame:
    """Đóng gói frame bất biến từ trạng thái engine hiện thời."""
    board = game.to_state().board
    remaining = {
        player: sum(
            piece is not None and piece.startswith(str(player))
            for row in board
            for piece in row
        )
        for player in (1, 2)
    }
    return ReplayFrame(
        ply,
        board,
        move,
        captured_piece,
        remaining[1],
        remaining[2],
    )


def cleanup_replay_files(
    replay_directory: str | Path,
    run_dates: Mapping[str, date],
    today: date | None = None,
    retention_days: int = config.REPLAY_RETENTION_DAYS,
    protected_run_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Xóa file run quá hạn, trừ run được giữ bảo toàn theo giải đấu."""
    if retention_days < 1:
        raise ValueError("retention_days phải lớn hơn 0")
    root = Path(replay_directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cutoff = (today or datetime.now(ZoneInfo(config.TIMEZONE_NAME)).date()) - timedelta(
        days=retention_days
    )
    protected = set(protected_run_ids)
    removed: list[str] = []
    for run_id, run_date in run_dates.items():
        if run_id in protected or run_date > cutoff:
            continue
        replay_path = (root / f"{run_id}.bin").resolve()
        if replay_path.parent != root:
            raise ValueError("run_id tạo đường dẫn replay không an toàn")
        try:
            replay_path.unlink()
            removed.append(run_id)
        except FileNotFoundError:
            continue
    return tuple(removed)
