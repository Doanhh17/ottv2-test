"""Kiểm thử seed, replay, ghép cặp và xếp hạng giải đấu."""

from uuid import UUID
import time

import pytest

from benchmark_bots import GreedyBot, RushBot
from engine import OTTGame
from tournament_runner import (
    BotEntrant,
    GameTask,
    GameParticipant,
    GameWorkerResult,
    PairingError,
    SwissStanding,
    TournamentRunner,
    assert_valid_ranking,
    derive_seed,
    pair_swiss,
    play_game,
)


def fake_game(task: GameTask) -> GameWorkerResult:
    """Trả kết quả ổn định giả lập để kiểm tra điều phối."""
    game_id = f"fake-{task.match_id}-{task.game_index}"
    return GameWorkerResult(
        game_id,
        b"\x00\x01",
        {
            "game_id": game_id,
            "run_id": task.run_id,
            "match_id": task.match_id,
            "game_index": task.game_index,
            "p1_id": task.p1.student_id,
            "p2_id": task.p2.student_id,
            "result": "P1",
            "win_reason": "P1 thắng giả lập",
            "plies": 1,
            "captured_p1": 1,
            "captured_p2": 0,
            "time_p1_seconds": 0.01,
            "time_p2_seconds": 0.01,
            "moves_p1": 1,
            "moves_p2": 0,
            "forfeiting_player": None,
        },
    )


def test_seed_is_stable_and_sensitive_to_run_or_match() -> None:
    assert derive_seed("run-a", "match-1") == derive_seed("run-a", "match-1")
    assert derive_seed("run-a", "match-1") != derive_seed("run-b", "match-1")
    assert derive_seed("run-a", "match-1") != derive_seed("run-a", "match-2")


def test_play_game_returns_compact_replay_and_repeatable_id() -> None:
    task = GameTask(
        "run-replay",
        "match-replay",
        0,
        _standard_participant("GreedyBot"),
        _standard_participant("RushBot"),
    )

    first_result = play_game(task)
    second_result = play_game(task)

    assert len(first_result.moves_bytes) == first_result.metadata["plies"] * 2
    assert str(UUID(first_result.game_id)) == first_result.game_id
    assert first_result.game_id != second_result.game_id
    assert first_result.metadata["result"] in ("P1", "P2", "DRAW")
    assert first_result.metadata["p1_id"] == "@GreedyBot"


def test_parallel_local_games_can_spawn_isolated_bot_workers() -> None:
    source = "def make_move(board, valid_moves, player): return valid_moves[0]\n"
    runner = TournamentRunner("run-threaded", workers=2)
    tasks = [
        GameTask(
            "run-threaded",
            f"match-{index}",
            index,
            GameParticipant(f"p1-{index}", source=source),
            GameParticipant(f"p2-{index}", source=source),
            max_plies=2,
        )
        for index in range(2)
    ]

    results = runner._run_games(tasks)

    assert len(results) == 2
    assert {result.metadata["game_index"] for result in results} == {0, 1}
    assert all(result.metadata["plies"] <= 2 for result in results)


def test_first_swiss_round_pairs_seed_halves() -> None:
    standings = [SwissStanding(f"s{seed}", 0, 0, 0, seed) for seed in range(1, 9)]

    pairs = pair_swiss(standings, {}, first_round=True)

    assert [(pair.first_id, pair.second_id) for pair in pairs] == [
        ("s1", "s5"),
        ("s2", "s6"),
        ("s3", "s7"),
        ("s4", "s8"),
    ]


def test_swiss_backtracking_avoids_every_previous_opponent() -> None:
    standings = [SwissStanding(f"s{seed}", 3, 0, 0, seed) for seed in range(1, 7)]
    previous = {
        "s1": {"s2", "s3"},
        "s2": {"s1", "s4"},
        "s3": {"s1", "s4"},
        "s4": {"s2", "s3"},
        "s5": set(),
        "s6": set(),
    }

    pairs = pair_swiss(standings, previous)

    assert len(pairs) == 3
    assert all(
        pair.second_id not in previous[pair.first_id]
        for pair in pairs
    )


def test_swiss_rejects_odd_field_instead_of_assigning_bye() -> None:
    standings = [SwissStanding(f"s{seed}", 0, 0, 0, seed) for seed in range(1, 4)]

    with pytest.raises(PairingError, match="chẵn"):
        pair_swiss(standings, {})


def test_double_elimination_and_full_tournament_have_unique_ranks() -> None:
    entrants = [
        BotEntrant(f"stu{index:02}", "def make_move(board, valid_moves, player): return valid_moves[0]")
        for index in range(1, 7)
    ]
    checkpoints: list[tuple[str, object]] = []
    runner = TournamentRunner(
        "run-test",
        workers=1,
        game_executor=fake_game,
        checkpoint=lambda stage, state: checkpoints.append((stage, state)),
    )

    summary = runner.run(
        entrants,
        gatekeeper_cutoff=4,
        swiss_rounds=2,
        playoff_cutoff=2,
    )

    assert_valid_ranking(summary.ranks, expected_count=6)
    assert len(summary.playoff_order) == 2
    assert checkpoints[0][0] == "gatekeeper"
    assert any(stage == "swiss_round_2" for stage, _ in checkpoints)
    assert checkpoints[-1][0] == "complete"
    assert len(summary.game_results) > 0


def test_double_elimination_four_player_bracket_orders_all_eliminations() -> None:
    entrants = [
        BotEntrant(f"s{index}", "def make_move(board, valid_moves, player): return valid_moves[0]")
        for index in range(1, 5)
    ]
    runner = TournamentRunner("run-playoff", workers=1, game_executor=fake_game)

    result = runner.run_double_elimination(entrants, ["s1", "s2", "s3", "s4"])

    assert result.ranking == ("s1", "s2", "s3", "s4")
    assert len(result.game_results) == 12


def test_tournament_uses_historical_seed_for_swiss_order() -> None:
    entrants = [
        BotEntrant(
            f"s{index}",
            "def make_move(board, valid_moves, player): return valid_moves[0]",
            historical_seed=float(10 - index),
        )
        for index in range(1, 5)
    ]
    runner = TournamentRunner("run-history", workers=1, game_executor=fake_game)
    pairings_seen: list[tuple[str, str]] = []
    original_swiss_tasks = runner._swiss_tasks

    def capture_pair(first: BotEntrant, second: BotEntrant, round_no: int):
        if round_no == 1:
            pairings_seen.append((first.student_id, second.student_id))
        return original_swiss_tasks(first, second, round_no)

    runner._swiss_tasks = capture_pair
    runner.run(entrants, gatekeeper_cutoff=4, swiss_rounds=1, playoff_cutoff=2)

    assert pairings_seen == [("s4", "s2"), ("s3", "s1")]


def test_pool_worker_runs_submission_in_its_own_process() -> None:
    source = "def make_move(board, valid_moves, player): return valid_moves[0]"
    task = GameTask(
        "run-pool",
        "match-pool",
        0,
        GameParticipant("student-a", source=source),
        GameParticipant("@GreedyBot", benchmark_kind="GreedyBot"),
        max_plies=1,
    )
    runner = TournamentRunner("run-pool", workers=2)

    results = runner._run_games([task])

    assert len(results) == 1
    assert results[0].metadata["p1_id"] == "student-a"
    assert results[0].metadata["result"] in ("P1", "P2", "DRAW")


def test_parallel_remote_game_executor_returns_all_results() -> None:
    tasks = [
        GameTask(
            "run-remote",
            "match-remote",
            index,
            GameParticipant(f"student-{index}"),
            GameParticipant(f"opponent-{index}"),
            max_plies=1,
        )
        for index in range(4)
    ]
    runner = TournamentRunner(
        "run-remote",
        workers=2,
        game_executor=fake_game,
        parallel_game_executor=True,
    )

    results = runner._run_games(tasks)

    assert len(results) == 4
    assert {result.metadata["game_index"] for result in results} == {0, 1, 2, 3}


def test_expired_deadline_returns_partial_contiguous_ranking() -> None:
    entrants = [
        BotEntrant(f"s{index}", "def make_move(board, valid_moves, player): return valid_moves[0]")
        for index in range(1, 5)
    ]
    checkpoints: list[str] = []
    runner = TournamentRunner(
        "run-partial",
        workers=1,
        game_executor=fake_game,
        checkpoint=lambda stage, state: checkpoints.append(stage),
        deadline_monotonic=time.monotonic() - 1,
    )

    summary = runner.run(
        entrants,
        gatekeeper_cutoff=4,
        swiss_rounds=1,
        playoff_cutoff=2,
    )

    assert summary.partial
    assert set(summary.ranks.values()) == {1, 2, 3, 4}
    assert summary.game_results == ()
    assert checkpoints[-1] == "partial"


def test_ranking_validator_rejects_duplicate_or_missing_ranks() -> None:
    with pytest.raises(ValueError, match="hoán vị"):
        assert_valid_ranking({"a": 1, "b": 1})
    with pytest.raises(ValueError, match="hoán vị"):
        assert_valid_ranking({"a": 1, "b": 3})


def _standard_participant(kind: str) -> GameParticipant:
    return GameParticipant(f"@{kind}", benchmark_kind=kind)