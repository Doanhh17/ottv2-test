"""Authenticated Windows-hosted API that runs bot jobs in Docker containers."""

from __future__ import annotations

import hmac
import json
import os
import subprocess
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool


app = FastAPI(title="OTTv2 isolated bot worker")
MAX_JOB_BYTES = 4 * 100 * 1024 + 65_536
MAX_RESPONSE_BYTES = 1_048_576
BOT_IMAGE = os.environ.get("OTTV2_BOT_WORKER_IMAGE", "ottv2-bot-worker:latest")
WORKER_TOKEN = os.environ.get("OTTV2_SANDBOX_TOKEN", "")
DOCKER_COMMAND = os.environ.get("OTTV2_DOCKER_COMMAND", "docker")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/{operation}")
async def execute(operation: str, request: Request) -> JSONResponse:
    if operation not in {"move", "game"}:
        return JSONResponse({"error": "Unknown worker operation"}, status_code=404)
    if not WORKER_TOKEN:
        return JSONResponse({"error": "Worker token is not configured"}, status_code=503)
    supplied_token = request.headers.get("authorization", "")
    expected_token = f"Bearer {WORKER_TOKEN}"
    if not hmac.compare_digest(
        supplied_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    ):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_JOB_BYTES:
                return JSONResponse({"error": "Worker payload exceeded the size limit"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "Invalid content length"}, status_code=400)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_JOB_BYTES:
            return JSONResponse({"error": "Worker payload exceeded the size limit"}, status_code=413)
    raw_body = bytes(body)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "Request must contain valid UTF-8 JSON"}, status_code=400)
    if not isinstance(payload, dict) or payload.get("operation") != operation:
        return JSONResponse({"error": "Worker operation does not match request path"}, status_code=400)

    command = [
        DOCKER_COMMAND,
        "run",
        "--rm",
        "--network",
        "none",
        "--pull=never",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "16",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1",
        "--user",
        "10001:10001",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
        BOT_IMAGE,
    ]
    try:
        return_code, output, output_exceeded = await run_in_threadpool(
            _run_docker_job, command, raw_body
        )
    except FileNotFoundError:
        return JSONResponse({"error": "Docker CLI was not found on the worker host"}, status_code=503)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Isolated bot container exceeded its time limit"}, status_code=504)
    except OSError as exc:
        return JSONResponse({"error": f"Could not start isolated Docker job: {exc}"}, status_code=503)

    if output_exceeded:
        return JSONResponse({"error": "Container response exceeded the size limit"}, status_code=502)
    try:
        result: Any = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "Isolated container returned invalid JSON"}, status_code=502)
    if not isinstance(result, dict):
        return JSONResponse({"error": "Isolated container response must be an object"}, status_code=502)
    if return_code != 0 and "error" not in result:
        return JSONResponse({"error": "Isolated container exited unexpectedly"}, status_code=502)
    return JSONResponse(result)


def _run_docker_job(
    command: list[str],
    request_body: bytes,
) -> tuple[int, bytes, bool]:
    """Run Docker while keeping container output bounded in host memory."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        process.wait()
        raise OSError("Docker process did not expose standard input/output")
    output = bytearray()
    output_exceeded = threading.Event()

    def collect_output() -> None:
        while chunk := process.stdout.read(65_536):
            if len(output) + len(chunk) > MAX_RESPONSE_BYTES:
                output_exceeded.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            output.extend(chunk)

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()
    try:
        process.stdin.write(request_body)
        process.stdin.close()
        return_code = process.wait(timeout=50)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    except OSError:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        reader.join()
        process.stdout.close()
    return return_code, bytes(output), output_exceeded.is_set()
