"""Worker một lượt bot, nhận một payload JSON qua stdin và trả kết quả JSON."""

import json
import sys
from typing import Any

import config
from engine import Move, Piece
from sandbox import run_bot


MAX_REQUEST_BYTES = config.BOT_FILE_SIZE_BYTES * 2 + 65_536


def execute_request(
    raw_request: bytes,
    timeout_seconds: float = config.BOT_MOVE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Kiểm tra payload, chạy bot bị giới hạn và chuyển nước đi về JSON."""
    if len(raw_request) > MAX_REQUEST_BYTES:
        return {"error": "Payload worker vượt giới hạn"}
    try:
        payload = json.loads(raw_request.decode("utf-8"))
        source = payload["source"]
        board_data = payload["board"]
        moves_data = payload["valid_moves"]
        player = payload["player"]
        random_seed = payload.get("seed", config.BOT_RANDOM_SEED)
        board = _parse_board(board_data)
        valid_moves = _parse_moves(moves_data)
        if not isinstance(source, str) or not isinstance(player, int) or player not in config.PLAYERS:
            raise ValueError("source hoặc player không hợp lệ")
        if not isinstance(random_seed, int) or isinstance(random_seed, bool):
            raise ValueError("seed phải là số nguyên")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return {"error": f"Payload không hợp lệ: {exc}"}

    result = run_bot(
        source,
        board,
        valid_moves,
        player,
        timeout_seconds=timeout_seconds,
        random_seed=random_seed,
    )
    if result.error is not None or result.move is None:
        return {
            "error": result.error or "Bot không trả nước đi",
            "timed_out": result.timed_out,
            "elapsed_seconds": result.elapsed_seconds,
        }
    return {
        "move": result.move,
        "timed_out": False,
        "elapsed_seconds": result.elapsed_seconds,
    }


def _parse_board(value: object) -> tuple[tuple[Piece | None, ...], ...]:
    """Đọc bàn 9x9, chỉ nhận ô trống hoặc quân dạng phe + loại."""
    if not isinstance(value, list) or len(value) != config.BOARD_SIZE:
        raise ValueError("board phải có đúng 9 hàng")
    rows: list[tuple[Piece | None, ...]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != config.BOARD_SIZE:
            raise ValueError("Mỗi hàng board phải có đúng 9 ô")
        parsed_row: list[Piece | None] = []
        for piece in row:
            if piece is None:
                parsed_row.append(None)
            elif (
                isinstance(piece, str)
                and len(piece) == 2
                and piece[0] in ("1", "2")
                and piece[1] in config.PIECE_TYPES
            ):
                parsed_row.append(piece)
            else:
                raise ValueError("Mã quân trên bàn không hợp lệ")
        rows.append(tuple(parsed_row))
    return tuple(rows)


def _parse_moves(value: object) -> list[Move]:
    """Đọc danh sách cặp tọa độ trong giới hạn bàn cờ."""
    if not isinstance(value, list) or len(value) > config.BOARD_SIZE**2 * 8:
        raise ValueError("valid_moves phải là danh sách hợp lệ")
    moves: list[Move] = []
    for raw_move in value:
        if not isinstance(raw_move, list) or len(raw_move) != 2:
            raise ValueError("Mỗi nước phải có tọa độ đi và đến")
        coordinates = []
        for coordinate in raw_move:
            if (
                not isinstance(coordinate, list)
                or len(coordinate) != 2
                or any(type(value) is not int for value in coordinate)
                or any(not 0 <= value < config.BOARD_SIZE for value in coordinate)
            ):
                raise ValueError("Tọa độ nước đi không hợp lệ")
            coordinates.append((coordinate[0], coordinate[1]))
        moves.append((coordinates[0], coordinates[1]))
    if not moves:
        raise ValueError("valid_moves không được rỗng")
    return moves


def main() -> int:
    """Đọc một request giới hạn kích thước và ghi đúng một JSON response."""
    request = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    response = execute_request(request)
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if "error" not in response else 1


if __name__ == "__main__":
    raise SystemExit(main())