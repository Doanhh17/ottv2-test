"""Container entry point for a single move or complete tournament game."""

from __future__ import annotations

import base64
import json
import sys
from typing import Any

import config
from sandbox_worker import execute_request
from tournament_runner import GameParticipant, GameTask, play_game


MAX_JOB_BYTES = config.BOT_FILE_SIZE_BYTES * 4 + 65_536


def execute_job(raw_job: bytes) -> dict[str, Any]:
    """Validate a bounded worker job and run it inside the isolated container."""
    if len(raw_job) > MAX_JOB_BYTES:
        return {"error": "Worker payload exceeded the size limit"}
    try:
        job = json.loads(raw_job.decode("utf-8"))
        if not isinstance(job, dict):
            raise ValueError("job must be an object")
        operation = job.get("operation")
        if operation == "move":
            timeout_seconds = job.get("timeout_seconds", config.BOT_MOVE_TIMEOUT_SECONDS)
            if (
                not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool)
                or not 0 < timeout_seconds <= config.BOT_GAME_TIMEOUT_SECONDS
            ):
                raise ValueError("timeout_seconds is out of range")
            return execute_request(
                json.dumps(
                    {
                        "source": job["source"],
                        "board": job["board"],
                        "valid_moves": job["valid_moves"],
                        "player": job["player"],
                        "seed": job.get("seed", config.BOT_RANDOM_SEED),
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8"),
                timeout_seconds=float(timeout_seconds),
            )
        if operation == "game":
            return _execute_game(job["task"])
        raise ValueError("operation must be move or game")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return {"error": f"Invalid worker job: {exc}"}


def _execute_game(raw_task: object) -> dict[str, Any]:
    if not isinstance(raw_task, dict):
        raise ValueError("task must be an object")
    run_id = raw_task.get("run_id")
    match_id = raw_task.get("match_id")
    game_index = raw_task.get("game_index")
    max_plies = raw_task.get("max_plies", config.MAX_PLIES)
    if (
        not isinstance(run_id, str)
        or not isinstance(match_id, str)
        or type(game_index) is not int
        or type(max_plies) is not int
        or not 1 <= max_plies <= config.MAX_PLIES
    ):
        raise ValueError("task metadata is invalid")
    task = GameTask(
        run_id=run_id,
        match_id=match_id,
        game_index=game_index,
        p1=_parse_participant(raw_task.get("p1")),
        p2=_parse_participant(raw_task.get("p2")),
        max_plies=max_plies,
    )
    result = play_game(task)
    return {
        "game_id": result.game_id,
        "moves": base64.b64encode(result.moves_bytes).decode("ascii"),
        "metadata": result.metadata,
    }


def _parse_participant(value: object) -> GameParticipant:
    if not isinstance(value, dict):
        raise ValueError("participant must be an object")
    student_id = value.get("student_id")
    source = value.get("source")
    benchmark_kind = value.get("benchmark_kind")
    submission_error = value.get("submission_error")
    if not isinstance(student_id, str):
        raise ValueError("participant student_id is invalid")
    if source is not None and not isinstance(source, str):
        raise ValueError("participant source is invalid")
    if benchmark_kind is not None and not isinstance(benchmark_kind, str):
        raise ValueError("participant benchmark_kind is invalid")
    if submission_error is not None and not isinstance(submission_error, str):
        raise ValueError("participant submission_error is invalid")
    return GameParticipant(
        student_id=student_id,
        source=source,
        benchmark_kind=benchmark_kind,
        submission_error=submission_error,
    )


def main() -> int:
    raw_job = sys.stdin.buffer.read(MAX_JOB_BYTES + 1)
    result = execute_job(raw_job)
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
