"""Ba bot tham chiếu để kiểm tra và xếp hạng bot dự thi."""

import random
from typing import Protocol, TypeAlias

import config
from engine import Coordinate, Move, Piece


Board: TypeAlias = tuple[tuple[Piece | None, ...], ...]


class BotStrategy(Protocol):
    """Giao diện chung của một chiến lược bot."""

    def make_move(
        self, board: Board, valid_moves: list[Move], player: int
    ) -> Move:
        """Chọn một nước trong danh sách nước hợp lệ."""


class RandomBot:
    """Chọn ngẫu nhiên nước hợp lệ với bộ sinh số tái lập được."""

    def __init__(self, seed: int = config.BOT_RANDOM_SEED) -> None:
        self._random = random.Random(seed)

    def make_move(self, board: Board, valid_moves: list[Move], player: int) -> Move:
        """Chọn ngẫu nhiên một nước hợp lệ."""
        del board, player
        if not valid_moves:
            raise ValueError("Không có nước đi hợp lệ")
        return self._random.choice(valid_moves)


class GreedyBot:
    """Ưu tiên ăn quân, sau đó tiến về căn cứ đối phương."""

    def make_move(self, board: Board, valid_moves: list[Move], player: int) -> Move:
        """Chọn nước ăn quân có thể thực hiện, nếu không thì tiến gần mục tiêu."""
        if not valid_moves:
            raise ValueError("Không có nước đi hợp lệ")
        goal = config.GOAL_BY_PLAYER[player]

        def score(move: Move) -> tuple[int, int, int, int, int]:
            _, target = move
            target_piece = board[target[0]][target[1]]
            capture = int(target_piece is not None)
            distance = _distance(target, goal)
            return capture, -distance, -target[0], -target[1], -_move_order(move)

        return max(valid_moves, key=score)


class RushBot:
    """Đưa quân gần mục tiêu nhất tiến nhanh, tránh ô bị đối phương ăn."""

    def make_move(self, board: Board, valid_moves: list[Move], player: int) -> Move:
        """Chọn nước tiến gần mục tiêu và ưu tiên không bị ăn ngay."""
        if not valid_moves:
            raise ValueError("Không có nước đi hợp lệ")
        goal = config.GOAL_BY_PLAYER[player]

        def score(move: Move) -> tuple[int, int, int, int, int, int]:
            source, target = move
            endangered = int(_is_threatened(board, target, player))
            source_distance = _distance(source, goal)
            target_distance = _distance(target, goal)
            return (
                -endangered,
                -source_distance,
                -target_distance,
                -target[0],
                -target[1],
                -_move_order(move),
            )

        return max(valid_moves, key=score)


def _distance(first: Coordinate, second: Coordinate) -> int:
    """Tính khoảng cách Chebyshev phù hợp với nước đi tám hướng."""
    return max(abs(first[0] - second[0]), abs(first[1] - second[1]))


def _is_threatened(board: Board, target: Coordinate, player: int) -> bool:
    """Kiểm tra có quân đối phương kề ô đích và thắng loại quân tại đó."""
    target_piece = board[target[0]][target[1]]
    if target_piece is None:
        return False
    target_type = target_piece[1]
    opponent = 3 - player
    for row_delta, col_delta in config.MOVE_OFFSETS:
        row = target[0] + row_delta
        col = target[1] + col_delta
        if 0 <= row < config.BOARD_SIZE and 0 <= col < config.BOARD_SIZE:
            attacker = board[row][col]
            if (
                attacker is not None
                and int(attacker[0]) == opponent
                and config.PIECE_BEATS[attacker[1]] == target_type
            ):
                return True
    return False


def _move_order(move: Move) -> int:
    """Tạo khóa phụ ổn định để xử lý trường hợp các nước bằng điểm."""
    source, target = move
    return source[0] * 10_000 + source[1] * 1_000 + target[0] * 10 + target[1]


def standard_bots(seed: int = config.BOT_RANDOM_SEED) -> dict[str, BotStrategy]:
    """Tạo bộ ba bot chuẩn với seed RandomBot có thể cấu hình."""
    return {
        "RandomBot": RandomBot(seed),
        "GreedyBot": GreedyBot(),
        "RushBot": RushBot(),
    }