"""Kiểm thử luật cơ bản của OTTv2."""

import pytest

from engine import IllegalMoveError, OTTGame


def set_position(
    game: OTTGame,
    pieces: list[tuple[int, int, str, int]],
    player_to_move: int = 1,
    plies: int = 0,
) -> None:
    """Dựng thế cờ nhỏ để kiểm tra từng luật độc lập."""
    game._board = [None] * 81
    for row, col, piece_type, player in pieces:
        game._board[row * 9 + col] = f"{player}{piece_type}"
    game.current_player = player_to_move
    game._plies = plies
    game._captured = {1: [0, 0, 0], 2: [0, 0, 0]}
    game._result = None
    game._win_reason = ""
    game._refresh_outcome()


def balanced_position() -> list[tuple[int, int, str, int]]:
    """Trả về thế cờ có đủ ba loại quân của cả hai phe."""
    return [
        (2, 2, "R", 1),
        (2, 3, "S", 1),
        (3, 2, "P", 1),
        (6, 6, "R", 2),
        (6, 5, "S", 2),
        (5, 6, "P", 2),
    ]


def test_initial_position_has_nine_pieces_per_player_and_empty_goals() -> None:
    game = OTTGame()
    state = game.to_state()

    assert sum(piece is not None for row in state.board for piece in row) == 18
    assert sum(piece is not None and piece.startswith("1") for row in state.board for piece in row) == 9
    assert sum(piece is not None and piece.startswith("2") for row in state.board for piece in row) == 9
    assert state.board[0][0] is None
    assert state.board[8][8] is None
    assert state.player_to_move == 1


def test_rock_captures_scissors_and_updates_capture_counter() -> None:
    game = OTTGame()
    set_position(
        game,
        [
            (2, 2, "R", 1),
            (1, 2, "S", 2),
            (7, 7, "R", 2),
            (7, 6, "P", 2),
            (3, 2, "S", 1),
            (3, 3, "P", 1),
        ],
    )

    game.apply_move(((2, 2), (1, 2)))

    assert game.to_state().board[1][2] == "1R"
    assert game.captured_p1 == (0, 1, 0)
    assert game.result() == "P1"
    assert "ăn hết quân loại S" in game.win_reason()


def test_piece_cannot_capture_type_that_beats_it() -> None:
    game = OTTGame()
    set_position(
        game,
        [
            (2, 2, "R", 1),
            (1, 2, "P", 2),
            (7, 7, "R", 2),
            (7, 6, "S", 2),
            (3, 2, "S", 1),
            (3, 3, "P", 1),
        ],
    )

    assert ((2, 2), (1, 2)) not in game.get_valid_moves(1)
    with pytest.raises(IllegalMoveError):
        game.apply_move(((2, 2), (1, 2)))


def test_friendly_piece_and_same_type_enemy_block_destination() -> None:
    game = OTTGame()
    set_position(
        game,
        [
            (3, 3, "R", 1),
            (3, 4, "P", 1),
            (4, 3, "R", 2),
            (7, 7, "S", 2),
            (7, 6, "P", 2),
            (2, 3, "S", 1),
            (2, 2, "P", 1),
        ],
    )

    moves = game.get_valid_moves(1)
    assert ((3, 3), (3, 4)) not in moves
    assert ((3, 3), (4, 3)) not in moves


def test_reaching_opponents_corner_wins() -> None:
    game = OTTGame()
    set_position(
        game,
        [
            (7, 7, "R", 1),
            (2, 2, "S", 1),
            (2, 3, "P", 1),
            (6, 6, "R", 2),
            (6, 5, "S", 2),
            (5, 6, "P", 2),
        ],
    )

    game.apply_move(((7, 7), (8, 8)))

    assert game.result() == "P1"
    assert "cắm cờ" in game.win_reason()


def test_eliminating_last_piece_of_a_type_wins() -> None:
    game = OTTGame()
    set_position(
        game,
        [
            (2, 2, "S", 1),
            (1, 2, "P", 2),
            (7, 7, "R", 2),
            (7, 6, "S", 2),
            (3, 2, "R", 1),
            (3, 3, "P", 1),
        ],
    )

    game.apply_move(((2, 2), (1, 2)))

    assert game.result() == "P1"
    assert "ăn hết quân loại P" in game.win_reason()


def test_draw_after_maximum_number_of_plies() -> None:
    game = OTTGame(max_plies=2)
    set_position(game, balanced_position())

    game.apply_move(((2, 2), (1, 1)))
    game.apply_move(((6, 6), (7, 7)))

    assert game.plies == 2
    assert game.result() == "DRAW"
    assert "đủ 2 nước đi" in game.win_reason()


def test_player_with_no_legal_move_loses() -> None:
    game = OTTGame()
    pieces = [
        (0, 8, "R", 1),
        (8, 0, "S", 1),
        (4, 4, "P", 1),
        (0, 7, "P", 2),
        (1, 7, "P", 2),
        (1, 8, "P", 2),
        (7, 0, "R", 2),
        (7, 1, "R", 2),
        (8, 1, "R", 2),
        (3, 3, "S", 2),
        (3, 4, "S", 2),
        (3, 5, "S", 2),
        (4, 3, "S", 2),
        (4, 5, "S", 2),
        (5, 3, "S", 2),
        (5, 4, "S", 2),
        (5, 5, "S", 2),
        (6, 8, "P", 2),
    ]
    set_position(game, pieces)

    assert game.get_valid_moves(1) == []
    assert game.is_over()
    assert game.result() == "P2"
    assert "không còn nước đi" in game.win_reason()


def test_both_players_winning_at_once_is_draw() -> None:
    game = OTTGame()
    set_position(
        game,
        [
            (8, 8, "R", 1),
            (0, 0, "S", 2),
            (2, 2, "R", 1),
            (2, 3, "S", 1),
            (2, 4, "P", 1),
            (6, 6, "R", 2),
            (6, 5, "S", 2),
            (5, 6, "P", 2),
        ],
    )

    assert game.result() == "DRAW"
    assert "cùng thỏa" in game.win_reason()


def test_state_is_immutable_and_copy_is_independent() -> None:
    game = OTTGame()
    snapshot = game.to_state()
    duplicate = game.copy()
    move = game.get_valid_moves(1)[0]

    with pytest.raises(TypeError):
        snapshot.board[0][0] = "1R"  # type: ignore[index]
    duplicate.apply_move(move)

    assert duplicate.plies == 1
    assert game.plies == 0
