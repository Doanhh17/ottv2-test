"""Luật và trạng thái ván đấu OTTv2."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

import config


Coordinate: TypeAlias = tuple[int, int]
Move: TypeAlias = tuple[Coordinate, Coordinate]
Piece: TypeAlias = str
GameResult: TypeAlias = Literal["P1", "P2", "DRAW"]


class IllegalMoveError(ValueError):
    """Nước đi không hợp lệ theo trạng thái hiện tại."""


@dataclass(frozen=True, slots=True)
class GameState:
    """Ảnh chụp bất biến của ván đấu, an toàn để chuyển cho bot."""

    board: tuple[tuple[Piece | None, ...], ...]
    player_to_move: int
    plies: int
    captured_p1: tuple[int, int, int]
    captured_p2: tuple[int, int, int]


class OTTGame:
    """Quản lý một ván Oẳn Tù Tì OTTv2."""

    def __init__(self, max_plies: int = config.MAX_PLIES) -> None:
        if max_plies < 1:
            raise ValueError("max_plies phải lớn hơn hoặc bằng 1")
        self.max_plies = max_plies
        self._board: list[Piece | None] = []
        self.current_player = config.INITIAL_PLAYER
        self._plies = 0
        self._captured: dict[int, list[int]] = {}
        self._result: GameResult | None = None
        self._win_reason = ""
        self.reset()

    def reset(self) -> None:
        """Khởi tạo bàn cờ, quân đã ăn và lượt đi."""
        self._board = [None] * (config.BOARD_SIZE * config.BOARD_SIZE)
        for (row, col), piece_type in config.INITIAL_LAYOUT_P1:
            self._board[self._index((row, col))] = f"1{piece_type}"
            mirrored = (config.BOARD_SIZE - 1 - row, config.BOARD_SIZE - 1 - col)
            self._board[self._index(mirrored)] = f"2{piece_type}"

        self.current_player = config.INITIAL_PLAYER
        self._plies = 0
        self._captured = {player: [0, 0, 0] for player in config.PLAYERS}
        self._result = None
        self._win_reason = ""
        self._refresh_outcome()

    def get_valid_moves(self, player: int) -> list[Move]:
        """Trả về các nước đi hợp lệ của phe được chỉ định."""
        self._validate_player(player)
        moves: list[Move] = []
        board_size = config.BOARD_SIZE

        for source_index, piece in enumerate(self._board):
            if piece is None or int(piece[0]) != player:
                continue
            row, col = divmod(source_index, board_size)
            for row_delta, col_delta in config.MOVE_OFFSETS:
                target = (row + row_delta, col + col_delta)
                target_row, target_col = target
                if not (0 <= target_row < board_size and 0 <= target_col < board_size):
                    continue
                target_piece = self._board[self._index(target)]
                if self._can_enter(piece, target_piece):
                    moves.append(((row, col), target))
        return moves

    def apply_move(self, move: Move) -> None:
        """Áp dụng một nước hợp lệ hoặc ném IllegalMoveError."""
        if self.is_over():
            raise IllegalMoveError("Ván đấu đã kết thúc")
        if not self._is_well_formed_move(move) or move not in self.get_valid_moves(
            self.current_player
        ):
            raise IllegalMoveError("Nước đi không hợp lệ")

        source, target = move
        source_index = self._index(source)
        target_index = self._index(target)
        moving_piece = self._board[source_index]
        target_piece = self._board[target_index]
        if moving_piece is None:
            raise IllegalMoveError("Ô xuất phát không có quân")

        if target_piece is not None:
            captured_type_index = config.PIECE_TYPES.index(target_piece[1])
            self._captured[self.current_player][captured_type_index] += 1
        self._board[target_index] = moving_piece
        self._board[source_index] = None
        self._plies += 1
        self.current_player = self._other_player(self.current_player)
        self._refresh_outcome()

    def is_over(self) -> bool:
        """Cho biết ván đã kết thúc hay phe đang đi không còn nước hợp lệ."""
        if self._result is not None:
            return True
        if self._plies >= self.max_plies:
            return True
        return not self.get_valid_moves(self.current_player)

    def result(self) -> GameResult:
        """Trả về phe thắng hoặc hòa; chỉ dùng sau khi ván kết thúc."""
        self._refresh_outcome()
        if self._result is None:
            raise ValueError("Ván đấu chưa kết thúc")
        return self._result

    def win_reason(self) -> str:
        """Trả về lý do kết thúc bằng tiếng Việt."""
        self._refresh_outcome()
        if self._result is None:
            raise ValueError("Ván đấu chưa kết thúc")
        return self._win_reason

    def copy(self) -> "OTTGame":
        """Sao chép nhanh trạng thái mà không dùng deepcopy."""
        duplicate = object.__new__(OTTGame)
        duplicate.max_plies = self.max_plies
        duplicate._board = self._board.copy()
        duplicate.current_player = self.current_player
        duplicate._plies = self._plies
        duplicate._captured = {
            player: counts.copy() for player, counts in self._captured.items()
        }
        duplicate._result = self._result
        duplicate._win_reason = self._win_reason
        return duplicate

    def to_state(self) -> GameState:
        """Tạo trạng thái 9x9 bất biến để cung cấp cho bot."""
        board = tuple(
            tuple(self._board[row * config.BOARD_SIZE : (row + 1) * config.BOARD_SIZE])
            for row in range(config.BOARD_SIZE)
        )
        return GameState(
            board=board,
            player_to_move=self.current_player,
            plies=self._plies,
            captured_p1=tuple(self._captured[1]),
            captured_p2=tuple(self._captured[2]),
        )

    @property
    def plies(self) -> int:
        """Số ply đã thực hiện trong ván."""
        return self._plies

    @property
    def captured_p1(self) -> tuple[int, int, int]:
        """Số quân mỗi loại mà P1 đã ăn, theo thứ tự R, S, P."""
        return tuple(self._captured[1])

    @property
    def captured_p2(self) -> tuple[int, int, int]:
        """Số quân mỗi loại mà P2 đã ăn, theo thứ tự R, S, P."""
        return tuple(self._captured[2])

    def _refresh_outcome(self) -> None:
        """Cập nhật kết quả theo điều kiện thắng và hòa."""
        if self._result is not None:
            return

        winning_reasons = {
            player: self._victory_reasons(player) for player in config.PLAYERS
        }
        winners = [player for player, reasons in winning_reasons.items() if reasons]

        if len(winners) == 2:
            self._result = "DRAW"
            self._win_reason = "Hòa: hai phe cùng thỏa điều kiện thắng"
        elif winners:
            winner = winners[0]
            self._result = f"P{winner}"  # type: ignore[assignment]
            self._win_reason = f"P{winner} thắng: {' và '.join(winning_reasons[winner])}"
        elif self._plies >= self.max_plies:
            self._result = "DRAW"
            self._win_reason = f"Hòa: đã đủ {self.max_plies} nước đi"
        elif not self.get_valid_moves(self.current_player):
            winner = self._other_player(self.current_player)
            self._result = f"P{winner}"  # type: ignore[assignment]
            self._win_reason = (
                f"P{self.current_player} thua vì không còn nước đi hợp lệ"
            )

    def _victory_reasons(self, player: int) -> list[str]:
        """Liệt kê các điều kiện thắng hiện tại của một phe."""
        opponent = self._other_player(player)
        present_types = {
            piece[1] for piece in self._board if piece is not None and int(piece[0]) == opponent
        }
        reasons: list[str] = []
        eliminated_types = [
            piece_type for piece_type in config.PIECE_TYPES if piece_type not in present_types
        ]
        if eliminated_types:
            labels = ", ".join(eliminated_types)
            reasons.append(f"ăn hết quân loại {labels} của đối phương")

        goal = config.GOAL_BY_PLAYER[player]
        goal_piece = self._board[self._index(goal)]
        if goal_piece is not None and int(goal_piece[0]) == player:
            reasons.append("cắm cờ ở căn cứ đối phương")
        return reasons

    @staticmethod
    def _can_enter(moving_piece: Piece, target_piece: Piece | None) -> bool:
        """Kiểm tra ô đích trống, ăn được, hay bị chặn."""
        if target_piece is None:
            return True
        if int(moving_piece[0]) == int(target_piece[0]):
            return False
        return config.PIECE_BEATS[moving_piece[1]] == target_piece[1]

    @staticmethod
    def _is_well_formed_move(move: object) -> bool:
        """Kiểm tra cấu trúc và giới hạn tọa độ của nước đi."""
        if not isinstance(move, tuple) or len(move) != 2:
            return False
        for coordinate in move:
            if not isinstance(coordinate, tuple) or len(coordinate) != 2:
                return False
            row, col = coordinate
            if type(row) is not int or type(col) is not int:
                return False
            if not (0 <= row < config.BOARD_SIZE and 0 <= col < config.BOARD_SIZE):
                return False
        return True

    @staticmethod
    def _validate_player(player: int) -> None:
        """Từ chối mã phe không hợp lệ."""
        if player not in config.PLAYERS:
            raise ValueError("player phải là 1 hoặc 2")

    @staticmethod
    def _other_player(player: int) -> int:
        """Trả về mã phe còn lại."""
        return 3 - player

    @staticmethod
    def _index(coordinate: Coordinate) -> int:
        """Đổi tọa độ hàng-cột thành chỉ số bàn cờ phẳng."""
        row, col = coordinate
        return row * config.BOARD_SIZE + col
