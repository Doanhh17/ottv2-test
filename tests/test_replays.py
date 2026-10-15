"""Kiểm thử replay nén, quota chọn ván và dựng frame OTTv2."""

import pytest
import zlib
import struct

from engine import OTTGame
from replays import (
    ReplayCandidate,
    ReplayFormatError,
    ReplayStore,
    build_replay_frames,
    choose_daily_replay_access,
    choose_replays,
    replay_quota,
    validate_moves_bytes,
)


def make_candidate(
    game_id: str,
    stage: int,
    round_no: int,
    opponent_strength: float = 0,
    plies: int = 10,
) -> ReplayCandidate:
    """Tạo fixture replay có cả hai người chơi."""
    return ReplayCandidate(
        game_id,
        "student-a",
        "student-b",
        stage,
        round_no,
        opponent_strength,
        plies,
        "P1",
        "P1 thắng",
    )


def test_replay_quota_matches_six_and_twenty_game_examples() -> None:
    assert replay_quota(6) == 2
    assert replay_quota(20) == 5
    assert replay_quota(0) == 1


def test_top32_replays_are_prioritized_and_quota_is_capped_at_five() -> None:
    candidates = [make_candidate(f"game-{index:02}", 1, 0) for index in range(15)]
    candidates.extend(make_candidate(f"playoff-{index:02}", 3, index) for index in range(5))

    choices = choose_replays("student-a", candidates)

    assert len(choices) == 5
    assert all(choice.game_id.startswith("playoff-") for choice in choices)


def test_six_games_get_two_access_entries() -> None:
    candidates = [make_candidate(f"game-{index}", 1, 0) for index in range(6)]

    choices = choose_replays("student-a", candidates)

    assert len(choices) == 2
    assert [choice.order_no for choice in choices] == [1, 2]


def test_user_picks_are_kept_first_then_auto_filled_to_quota() -> None:
    candidates = [make_candidate(f"game-{index}", 1, 0) for index in range(8)]

    choices = choose_replays(
        "student-a", candidates, mode="user", selected_game_ids=["game-6"]
    )

    assert len(choices) == 2
    assert choices[0].game_id == "game-6"


def test_daily_access_selects_games_independently_for_both_players() -> None:
    candidates = [make_candidate(f"game-{index}", 2, index) for index in range(6)]

    choices = choose_daily_replay_access(candidates, ["student-a", "student-b"])

    assert len(choices) == 4
    assert {choice.student_id for choice in choices} == {"student-a", "student-b"}


def test_replay_store_appends_and_reads_compressed_records(tmp_path) -> None:
    store = ReplayStore(tmp_path / "run-1.bin")
    first = store.append(b"\x00\x01")
    second = store.append(b"\x02\x03\x04\x05")

    assert store.read(*first) == b"\x00\x01"
    assert store.read(*second) == b"\x02\x03\x04\x05"
    assert second[0] == first[0] + first[1]


def test_replay_store_rejects_excessive_decompressed_record(tmp_path) -> None:
    path = tmp_path / "run-large.bin"
    compressed = zlib.compress(bytes(1000))
    record = struct.pack(">I", len(compressed)) + compressed
    path.write_bytes(record)
    store = ReplayStore(path)

    with pytest.raises(ReplayFormatError, match="quá lớn"):
        store.read(0, len(record))


def test_replay_frame_builder_applies_real_engine_rules() -> None:
    game = OTTGame()
    moves = game.get_valid_moves(1)
    move = moves[0]
    source = move[0][0] * 9 + move[0][1]
    target = move[1][0] * 9 + move[1][1]

    frames = build_replay_frames(bytes((source, target)))

    assert len(frames) == 2
    assert frames[0].ply == 0
    assert frames[1].ply == 1
    assert frames[1].move == move
    assert frames[1].remaining_p1 == 9
    assert frames[1].remaining_p2 == 9


def test_replay_validator_rejects_odd_length_out_of_range_and_too_many_plies() -> None:
    with pytest.raises(ReplayFormatError):
        validate_moves_bytes(b"\x00")
    with pytest.raises(ReplayFormatError):
        validate_moves_bytes(bytes((0, 81)))
    with pytest.raises(ReplayFormatError):
        validate_moves_bytes(bytes(302))


def test_frame_builder_rejects_an_illegal_encoded_move() -> None:
    with pytest.raises(ReplayFormatError, match="không hợp lệ"):
        build_replay_frames(bytes((0, 80)))
