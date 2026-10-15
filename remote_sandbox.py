"""HTTP client for the isolated Docker worker used by hosted deployments."""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import config

if TYPE_CHECKING:
    from tournament_runner import GameParticipant, GameTask, GameWorkerResult


class RemoteSandboxError(RuntimeError):
    """The remote bot worker did not return a valid execution result."""


def execute_move(
    source: str,
    board: tuple[tuple[str | None, ...], ...],
    valid_moves: list[tuple[tuple[int, int], tuple[int, int]]],
    player: int,
    timeout_seconds: float,
    random_seed: int,
) -> tuple[dict[str, Any], float]:
    """Ask the remote worker to execute one bot move in an isolated container."""
    started_at = time.perf_counter()
    payload = {
        "operation": "move",
        "source": source,
        "board": board,
        "valid_moves": valid_moves,
        "player": player,
        "seed": random_seed,
        "timeout_seconds": timeout_seconds,
    }
    result = _post("/v1/move", payload, timeout_seconds + 15)
    return result, time.perf_counter() - started_at


def execute_game(task: GameTask) -> GameWorkerResult:
    """Run a complete tournament game remotely and return the normal result type."""
    payload = {
        "operation": "game",
        "task": {
            "run_id": task.run_id,
            "match_id": task.match_id,
            "game_index": task.game_index,
            "max_plies": task.max_plies,
            "p1": _participant_payload(task.p1),
            "p2": _participant_payload(task.p2),
        },
    }
    result = _post("/v1/game", payload, config.BOT_GAME_TIMEOUT_SECONDS * 4 + 20)
    try:
        moves_bytes = base64.b64decode(result["moves"], validate=True)
        metadata = result["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        game_id = result["game_id"]
        if not isinstance(game_id, str):
            raise ValueError("game_id must be a string")
    except (KeyError, TypeError, ValueError) as exc:
        raise RemoteSandboxError("Worker returned a malformed game result") from exc
    from tournament_runner import GameWorkerResult

    return GameWorkerResult(game_id, moves_bytes, metadata)


def _participant_payload(participant: GameParticipant) -> dict[str, Any]:
    return {
        "student_id": participant.student_id,
        "source": participant.source,
        "benchmark_kind": participant.benchmark_kind,
        "submission_error": participant.submission_error,
    }


def _post(path: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    """POST JSON with the worker token and reject invalid or oversized responses."""
    if not config.SANDBOX_URL or not config.SANDBOX_TOKEN:
        raise RemoteSandboxError("Remote sandbox is not configured")
    request = Request(
        f"{config.SANDBOX_URL}{path}",
        data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.SANDBOX_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_response = response.read(1_048_577)
    except HTTPError as exc:
        raise RemoteSandboxError(f"Remote sandbox returned HTTP {exc.code}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise RemoteSandboxError(f"Remote sandbox request failed: {exc}") from exc
    if len(raw_response) > 1_048_576:
        raise RemoteSandboxError("Remote sandbox response exceeded 1 MiB")
    try:
        result = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteSandboxError("Remote sandbox returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RemoteSandboxError("Remote sandbox response must be a JSON object")
    if "error" in result:
        raise RemoteSandboxError(str(result["error"]))
    return result
