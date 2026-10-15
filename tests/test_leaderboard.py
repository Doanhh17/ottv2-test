"""Kiểm thử tổng hợp BXH và xuất JSON atomic."""

import json

from leaderboard import (
    DailyRankRecord,
    build_leaderboard,
    export_leaderboards,
    leaderboard_payload,
    write_json_atomic,
)


def test_absent_days_are_rank_4000_but_not_counted_as_played() -> None:
    entries = build_leaderboard(
        ["a", "b", "c"],
        [
            DailyRankRecord("a", 1, 1, top32=True),
            DailyRankRecord("a", 2, 10),
            DailyRankRecord("b", 1, 3),
            DailyRankRecord("b", 2, 3),
        ],
        [1, 2],
        "tournament",
    )

    by_id = {entry.student_id: entry for entry in entries}
    assert by_id["a"].average_rank == 5.5
    assert by_id["a"].days_played == 2
    assert by_id["c"].average_rank == 4000
    assert by_id["c"].days_played == 0
    assert by_id["c"].absent


def test_round_average_uses_only_expected_days_in_selected_round() -> None:
    entries = build_leaderboard(
        ["a", "b"],
        [
            DailyRankRecord("a", 1, 2),
            DailyRankRecord("a", 2, 20),
            DailyRankRecord("a", 8, 4),
            DailyRankRecord("b", 1, 3),
            DailyRankRecord("b", 8, 1),
        ],
        [1, 2, 8],
        "round",
        round_days=[1, 2],
    )

    by_id = {entry.student_id: entry for entry in entries}
    assert by_id["a"].average_rank == 11
    assert by_id["b"].average_rank == 2001.5
    assert by_id["a"].days_played == 2
    assert by_id["b"].days_played == 1


def test_ties_use_top32_then_latest_rank_then_student_id() -> None:
    entries = build_leaderboard(
        ["z", "b", "a"],
        [
            DailyRankRecord("z", 1, 5, top32=True),
            DailyRankRecord("b", 1, 5, top32=True),
            DailyRankRecord("a", 1, 5),
        ],
        [1],
        "daily",
    )

    assert [entry.student_id for entry in entries] == ["b", "z", "a"]


def test_daily_trend_compares_latest_rank_with_previous_day() -> None:
    entries = build_leaderboard(
        ["improving", "falling", "steady"],
        [
            DailyRankRecord("improving", 1, 10),
            DailyRankRecord("improving", 2, 4),
            DailyRankRecord("falling", 1, 2),
            DailyRankRecord("falling", 2, 9),
            DailyRankRecord("steady", 1, 7),
            DailyRankRecord("steady", 2, 7),
        ],
        [1, 2],
        "daily",
    )

    trends = {entry.student_id: entry.trend for entry in entries}
    assert trends == {"improving": "up", "falling": "down", "steady": "flat"}


def test_atomic_json_writer_replaces_valid_document(tmp_path) -> None:
    target = tmp_path / "leaderboard.json"
    write_json_atomic(target, {"value": "old"})
    write_json_atomic(target, {"value": "mới"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"value": "mới"}
    assert list(tmp_path.iterdir()) == [target]


def test_export_writes_daily_round_tournament_and_meta(tmp_path) -> None:
    outputs = export_leaderboards(
        {"entries": []},
        {1: {"entries": []}},
        {"entries": []},
        {"partial": False},
        tmp_path,
    )

    assert {path.name for path in outputs} == {
        "daily.json",
        "round_1.json",
        "tournament.json",
        "meta.json",
    }
