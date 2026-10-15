"""Tests for hosted storage and the isolated Docker job entry point."""

import base64
from io import BytesIO
import json

from fastapi.testclient import TestClient

from bot_job import execute_job
from engine import OTTGame
from object_storage import ObjectStorage
import sandbox_service


def test_worker_move_job_returns_a_legal_move() -> None:
    game = OTTGame()
    state = game.to_state()
    valid_moves = game.get_valid_moves(1)
    payload = {
        "operation": "move",
        "source": "def make_move(board, valid_moves, player): return valid_moves[0]",
        "board": state.board,
        "valid_moves": valid_moves,
        "player": 1,
        "seed": 123,
        "timeout_seconds": 2,
    }

    result = execute_job(json.dumps(payload).encode("utf-8"))

    assert "error" not in result
    returned_move = tuple(tuple(point) for point in result["move"])
    assert returned_move in valid_moves


def test_object_storage_uses_local_files_when_s3_is_not_configured(tmp_path) -> None:
    storage = ObjectStorage()
    root = tmp_path / "private"
    path = root / "bot.py"
    root.mkdir()
    path.write_text("def make_move(): return None", encoding="utf-8")

    assert not storage.enabled
    assert storage.read_path(path, root) == b"def make_move(): return None"
    assert storage.ensure_local(path, root) == path
    storage.put_path(path, root, "text/x-python")


def test_object_storage_round_trips_remote_files(monkeypatch, tmp_path) -> None:
    class FakeS3:
        objects = {}

        def put_object(self, Bucket, Key, Body, ContentType):
            self.objects[(Bucket, Key)] = Body.read()

        def get_object(self, Bucket, Key):
            return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    fake_client = FakeS3()
    monkeypatch.setattr("object_storage.boto3.client", lambda *args, **kwargs: fake_client)
    storage = ObjectStorage("https://objects.example", "private", "access", "secret")
    root = tmp_path / "submissions"
    path = root / "student" / "version_1.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bot source")

    storage.put_path(path, root, "text/x-python")
    path.unlink()

    assert storage.read_path(path, root) == b"bot source"
    assert storage.ensure_local(path, root).read_bytes() == b"bot source"


def test_worker_game_job_returns_a_game_result() -> None:
    payload = {
        "operation": "game",
        "task": {
            "run_id": "test-run",
            "match_id": "test-match",
            "game_index": 0,
            "max_plies": 1,
            "p1": {"student_id": "@GreedyBot", "benchmark_kind": "GreedyBot"},
            "p2": {"student_id": "@RushBot", "benchmark_kind": "RushBot"},
        },
    }

    result = execute_job(json.dumps(payload).encode("utf-8"))

    assert "error" not in result
    assert isinstance(result["game_id"], str)
    assert len(base64.b64decode(result["moves"], validate=True)) == 2
    assert result["metadata"]["match_id"] == "test-match"


def test_worker_http_requires_token_and_starts_restricted_container(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_service, "WORKER_TOKEN", "unit-test-token")
    calls = []

    def fake_run(command, request_body):
        calls.append((command, request_body))
        return 0, b'{"move":[[0,1],[0,0]]}', False

    monkeypatch.setattr(sandbox_service, "_run_docker_job", fake_run)
    client = TestClient(sandbox_service.app)
    try:
        unauthorized = client.post("/v1/move", json={"operation": "move"})
        assert unauthorized.status_code == 401
        assert calls == []

        response = client.post(
            "/v1/move",
            headers={"Authorization": "Bearer unit-test-token"},
            json={"operation": "move"},
        )

        assert response.status_code == 200
        assert response.json()["move"] == [[0, 1], [0, 0]]
        command, request_body = calls[0]
        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert "--cap-drop" in command
        assert not any(argument in {"-v", "--volume"} for argument in command)
        assert "--pull=never" in command
        assert b"unit-test-token" not in request_body
    finally:
        client.close()
