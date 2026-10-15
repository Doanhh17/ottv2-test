"""Kiểm thử quét mã nguồn và cách ly cơ bản của sandbox bot."""

import json

import config
from benchmark_bots import GreedyBot, RandomBot, RushBot
from engine import OTTGame
from sandbox import BotSandbox, run_bot, scan_bot_source
from sandbox_worker import execute_request


def test_scanner_accepts_valid_bot_and_whitelisted_import() -> None:
    result = scan_bot_source(
        "import math\n"
        "def make_move(board, valid_moves, player):\n"
        "    return valid_moves[int(math.sqrt(0))]\n"
    )

    assert result.valid
    assert result.errors == ()


def test_scanner_rejects_dangerous_imports_and_aliases() -> None:
    for source in (
        "import os\ndef make_move(board, valid_moves, player): return valid_moves[0]\n",
        "import subprocess as math\ndef make_move(board, valid_moves, player): return valid_moves[0]\n",
        "from socket import socket\ndef make_move(board, valid_moves, player): return valid_moves[0]\n",
        "from random import _os\ndef make_move(board, valid_moves, player): return valid_moves[0]\n",
        "import random as __builtins__\ndef make_move(board, valid_moves, player): return valid_moves[0]\n",
    ):
        result = scan_bot_source(source)
        assert not result.valid
        assert any("không được phép" in error for error in result.errors)


def test_scanner_rejects_unsafe_builtins_and_private_attribute_access() -> None:
    sources = (
        "def make_move(board, valid_moves, player): return open('x')\n",
        "def make_move(board, valid_moves, player): return valid_moves[0].__class__\n",
        "def make_move(board, valid_moves, player): return __import__('os')\n",
    )

    assert all(not scan_bot_source(source).valid for source in sources)


def test_scanner_rejects_syntax_errors_and_missing_api() -> None:
    assert not scan_bot_source("def make_move(:\n").valid
    assert not scan_bot_source("def choose(board): return None\n").valid


def test_worker_returns_a_legal_move_with_configured_game_timeout() -> None:
    game = OTTGame()
    state = game.to_state()
    valid_moves = game.get_valid_moves(1)
    source = (
        "def make_move(board, valid_moves, player):\n"
        "    goal = (8, 8) if player == 1 else (0, 0)\n"
        "    for move in valid_moves:\n"
        "        if move[1] == goal:\n"
        "            return move\n"
        "    return valid_moves[0]\n"
    )

    result = run_bot(
        source,
        state.board,
        valid_moves,
        1,
        timeout_seconds=config.BOT_GAME_TIMEOUT_SECONDS,
    )

    assert result.error is None
    assert result.move in valid_moves
    assert not result.timed_out


def test_worker_startup_time_counts_against_timeout_budget() -> None:
    game = OTTGame()
    result = run_bot(
        "def make_move(board, valid_moves, player): return valid_moves[0]\n",
        game.to_state().board,
        game.get_valid_moves(1),
        1,
        timeout_seconds=1e-9,
    )

    assert result.move is None
    assert result.timed_out


def test_container_worker_accepts_bounded_json_and_returns_json_move() -> None:
    game = OTTGame()
    state = game.to_state()
    valid_moves = game.get_valid_moves(1)
    request = json.dumps(
        {
            "source": "def make_move(board, valid_moves, player): return valid_moves[0]",
            "board": state.board,
            "valid_moves": valid_moves,
            "player": 1,
            "seed": 17,
        }
    ).encode("utf-8")

    response = execute_request(request, timeout_seconds=5.0)

    assert "error" not in response
    assert response["move"] == valid_moves[0]


def test_worker_randomness_is_reproducible_with_same_seed() -> None:
    game = OTTGame()
    source = (
        "import random\n"
        "def make_move(board, valid_moves, player):\n"
        "    return random.choice(valid_moves)\n"
    )
    board = game.to_state().board
    valid_moves = game.get_valid_moves(1)

    first = run_bot(source, board, valid_moves, 1, timeout_seconds=5.0, random_seed=91)
    second = run_bot(source, board, valid_moves, 1, timeout_seconds=5.0, random_seed=91)

    assert first.error is None
    assert first.move == second.move


def test_worker_rejects_an_illegal_move() -> None:
    game = OTTGame()
    state = game.to_state()
    source = "def make_move(board, valid_moves, player):\n    return ((0, 0), (8, 8))\n"

    result = run_bot(source, state.board, game.get_valid_moves(1), 1, timeout_seconds=5.0)

    assert result.move is None
    assert "không thuộc valid_moves" in (result.error or "")


def test_worker_kills_bot_that_exceeds_timeout() -> None:
    game = OTTGame()
    source = "def make_move(board, valid_moves, player):\n    while True: pass\n"

    result = run_bot(source, game.to_state().board, game.get_valid_moves(1), 1, timeout_seconds=0.5)

    assert result.timed_out
    assert result.move is None


def test_sandbox_enforces_total_game_timeout() -> None:
    game = OTTGame()
    source = "def make_move(board, valid_moves, player):\n    return valid_moves[0]\n"
    sandbox = BotSandbox(source, game_timeout_seconds=1e-9)

    result = sandbox.run_move(game.to_state().board, game.get_valid_moves(1), 1)

    assert result.timed_out
    assert "tổng thời gian" in (result.error or "")


def test_standard_bots_choose_only_legal_moves_and_random_is_reproducible() -> None:
    game = OTTGame()
    state = game.to_state()
    valid_moves = game.get_valid_moves(1)

    assert RandomBot(seed=7).make_move(state.board, valid_moves, 1) == RandomBot(
        seed=7
    ).make_move(state.board, valid_moves, 1)
    assert GreedyBot().make_move(state.board, valid_moves, 1) in valid_moves
    assert RushBot().make_move(state.board, valid_moves, 1) in valid_moves