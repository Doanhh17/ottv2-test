"""Quét và chạy bot không tin cậy trong tiến trình con hạn chế."""

import ast
import builtins
import contextlib
import hashlib
import io
import multiprocessing
import random
import time
from dataclasses import dataclass
from typing import Any

import config
from engine import Move, Piece

try:
    import resource
except ImportError:
    resource = None


Board = tuple[tuple[Piece | None, ...], ...]


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Kết quả kiểm tra mã nguồn bot trước khi thực thi."""

    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BotExecutionResult:
    """Kết quả chạy bot cùng thông tin lỗi hoặc thời gian thực thi."""

    move: Move | None
    error: str | None
    elapsed_seconds: float
    timed_out: bool = False


class BotSandbox:
    """Theo dõi giới hạn thời gian nước đi và tổng thời gian một ván."""

    def __init__(
        self,
        source: str,
        move_timeout_seconds: float = config.BOT_MOVE_TIMEOUT_SECONDS,
        game_timeout_seconds: float = config.BOT_GAME_TIMEOUT_SECONDS,
        seed: int = config.BOT_RANDOM_SEED,
    ) -> None:
        if move_timeout_seconds <= 0 or game_timeout_seconds <= 0:
            raise ValueError("Các giới hạn thời gian phải lớn hơn 0")
        self.source = source
        self.move_timeout_seconds = move_timeout_seconds
        self.game_timeout_seconds = game_timeout_seconds
        self.seed = seed
        self._move_index = 0
        self._started_at = time.perf_counter()

    @property
    def elapsed_seconds(self) -> float:
        """Tổng thời gian đã trôi qua kể từ khi bắt đầu ván."""
        return time.perf_counter() - self._started_at

    def run_move(self, board: Board, valid_moves: list[Move], player: int) -> BotExecutionResult:
        """Chạy một nước, giới hạn theo ngân sách còn lại của cả ván."""
        remaining_game_time = self.game_timeout_seconds - self.elapsed_seconds
        if remaining_game_time <= 0:
            return BotExecutionResult(
                None,
                "Bot vượt quá tổng thời gian cho phép mỗi ván",
                self.elapsed_seconds,
                timed_out=True,
            )
        result = run_bot(
            self.source,
            board,
            valid_moves,
            player,
            timeout_seconds=min(self.move_timeout_seconds, remaining_game_time),
            random_seed=self._next_seed(),
        )
        if self.elapsed_seconds >= self.game_timeout_seconds and not result.timed_out:
            return BotExecutionResult(
                None,
                "Bot vượt quá tổng thời gian cho phép mỗi ván",
                result.elapsed_seconds,
                timed_out=True,
            )
        return result

    def _next_seed(self) -> int:
        """Tạo seed ổn định riêng cho từng nước trong cùng trận."""
        material = f"{self.seed}:{self._move_index}".encode("ascii")
        self._move_index += 1
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def scan_bot_source(source: str) -> ScanResult:
    """Quét cú pháp, API, kích thước và các tên/import bị cấm."""
    errors: list[str] = []
    if not isinstance(source, str):
        return ScanResult(False, ("Mã nguồn phải là văn bản UTF-8",))
    if len(source.encode("utf-8")) > config.BOT_FILE_SIZE_BYTES:
        errors.append("Tệp bot vượt quá giới hạn 100 KiB")
    try:
        tree = ast.parse(source, filename="submission.py", mode="exec")
    except (SyntaxError, ValueError) as exc:
        return ScanResult(False, tuple(errors + [f"Lỗi cú pháp: {exc}"]))

    nodes = list(ast.walk(tree))
    if len(nodes) > config.BOT_MAX_AST_NODES:
        errors.append("Mã nguồn vượt quá giới hạn độ phức tạp AST")

    move_function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "make_move"
        ),
        None,
    )
    if not isinstance(move_function, ast.FunctionDef):
        errors.append("Phải định nghĩa hàm đồng bộ make_move(board, valid_moves, player)")
    elif (
        len(move_function.args.posonlyargs) + len(move_function.args.args) != 3
        or move_function.args.kwonlyargs
        or move_function.args.vararg is not None
        or move_function.args.kwarg is not None
        or move_function.args.defaults
        or move_function.args.kw_defaults
    ):
        errors.append("make_move phải nhận đúng ba tham số bắt buộc")

    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported_names = [
                alias.name.split(".")[0]
                for alias in node.names
                if isinstance(node, ast.Import)
            ]
            if isinstance(node, ast.ImportFrom):
                imported_names = [node.module.split(".")[0]] if node.module else []
            for imported_name in imported_names:
                if imported_name not in config.BOT_ALLOWED_IMPORTS:
                    errors.append(f"Import không được phép: {imported_name}")
            for alias in node.names:
                if (
                    alias.name == "*"
                    or alias.name.startswith("_")
                    or alias.name in config.BOT_FORBIDDEN_NAMES
                    or (alias.asname is not None and alias.asname.startswith("_"))
                    or (alias.asname is not None and alias.asname in config.BOT_FORBIDDEN_NAMES)
                ):
                    errors.append(f"Tên import không được phép: {alias.name}")
        elif isinstance(node, ast.Name) and (
            node.id in config.BOT_FORBIDDEN_NAMES or node.id.startswith("_")
        ):
            errors.append(f"Tên không được phép: {node.id}")
        elif isinstance(node, ast.Attribute) and (
            node.attr.startswith("_") or node.attr in config.BOT_FORBIDDEN_NAMES
        ):
            errors.append(f"Truy cập thuộc tính không được phép: {node.attr}")

    unique_errors = tuple(dict.fromkeys(errors))
    return ScanResult(not unique_errors, unique_errors)


def run_bot(
    source: str,
    board: Board,
    valid_moves: list[Move],
    player: int,
    timeout_seconds: float = config.BOT_MOVE_TIMEOUT_SECONDS,
    random_seed: int = config.BOT_RANDOM_SEED,
) -> BotExecutionResult:
    """Chạy bot trong tiến trình spawn riêng với giới hạn thời gian cứng."""
    scan = scan_bot_source(source)
    if not scan.valid:
        return BotExecutionResult(None, "; ".join(scan.errors), 0.0)
    if timeout_seconds <= 0:
        return BotExecutionResult(None, "Thời gian chạy phải lớn hơn 0", 0.0)
    if config.SANDBOX_URL:
        from remote_sandbox import RemoteSandboxError, execute_move

        remote_started = time.perf_counter()
        try:
            response, elapsed = execute_move(
                source,
                board,
                valid_moves,
                player,
                timeout_seconds,
                random_seed,
            )
            if "move" not in response:
                return BotExecutionResult(
                    None,
                    str(response.get("error", "Bot không trả nước đi")),
                    elapsed,
                    timed_out=bool(response.get("timed_out", False)),
                )
            move_data = response["move"]
            if (
                not isinstance(move_data, list)
                or len(move_data) != 2
                or any(not isinstance(point, list) or len(point) != 2 for point in move_data)
                or any(type(coordinate) is not int for point in move_data for coordinate in point)
            ):
                return BotExecutionResult(None, "Remote worker trả nước đi không hợp lệ", elapsed)
            move: Move = (
                (move_data[0][0], move_data[0][1]),
                (move_data[1][0], move_data[1][1]),
            )
            if not _is_valid_return(move, valid_moves):
                return BotExecutionResult(None, "Remote worker trả nước đi không hợp lệ", elapsed)
            return BotExecutionResult(move, None, elapsed)
        except RemoteSandboxError as exc:
            return BotExecutionResult(None, str(exc), time.perf_counter() - remote_started)

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_bot_worker,
        args=(sender, source, board, valid_moves, player, random_seed),
        daemon=True,
    )
    started_at = time.perf_counter()
    try:
        process.start()
        sender.close()
        process.join(timeout_seconds)
        if process.is_alive():
            process.kill()
            process.join()
            return BotExecutionResult(
                None,
                "Bot vượt quá thời gian cho phép",
                time.perf_counter() - started_at,
                timed_out=True,
            )
        if not receiver.poll():
            return BotExecutionResult(
                None,
                f"Tiến trình bot kết thúc bất thường (mã {process.exitcode})",
                time.perf_counter() - started_at,
            )
        status, payload = receiver.recv()
        elapsed = time.perf_counter() - started_at
        if status != "ok":
            return BotExecutionResult(None, str(payload), elapsed)
        return BotExecutionResult(payload, None, elapsed)
    except (OSError, RuntimeError, EOFError) as exc:
        if process.is_alive():
            process.kill()
            process.join()
        return BotExecutionResult(None, f"Không chạy được bot: {exc}", time.perf_counter() - started_at)
    finally:
        receiver.close()
        if sender:
            sender.close()


def _bot_worker(
    sender: Any,
    source: str,
    board: Board,
    valid_moves: list[Move],
    player: int,
    random_seed: int,
) -> None:
    """Áp giới hạn hệ điều hành rồi tải bot bằng builtins giới hạn."""
    try:
        _apply_resource_limits()
        random.seed(random_seed)
        safe_builtins = _restricted_builtins()
        namespace: dict[str, Any] = {
            "__builtins__": safe_builtins,
            "__name__": "bot_submission",
        }
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = compile(source, "submission.py", "exec", dont_inherit=True, optimize=2)
            exec(code, namespace, namespace)
            make_move = namespace.get("make_move")
            if not callable(make_move):
                raise ValueError("Không tìm thấy hàm make_move")
            move = make_move(board, valid_moves, player)
        if not _is_valid_return(move, valid_moves):
            raise ValueError("Bot trả về nước đi không thuộc valid_moves")
        sender.send(("ok", move))
    except BaseException as exc:
        try:
            sender.send(("error", f"{type(exc).__name__}: {exc}"[:500]))
        except (BrokenPipeError, OSError):
            pass
    finally:
        sender.close()


def _restricted_builtins() -> dict[str, Any]:
    """Chỉ cung cấp các hàm cơ bản và importer theo whitelist."""
    allowed_names = (
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "print",
        "range",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "Exception",
        "ValueError",
        "TypeError",
        "IndexError",
        "KeyError",
        "RuntimeError",
    )
    safe = {name: getattr(builtins, name) for name in allowed_names}
    safe["__import__"] = _restricted_import
    return safe


def _restricted_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """Chỉ cho phép import tuyệt đối các module đã duyệt."""
    if level != 0 or name not in config.BOT_ALLOWED_IMPORTS:
        raise ImportError(f"Import không được phép: {name}")
    return builtins.__import__(name, globals, locals, fromlist, level)


def _apply_resource_limits() -> None:
    """Áp dụng resource limits khả dụng trên hệ điều hành hiện tại."""
    if resource is None:
        return
    limits = (
        ("RLIMIT_CPU", config.BOT_CPU_SOFT_LIMIT_SECONDS, config.BOT_CPU_HARD_LIMIT_SECONDS),
        ("RLIMIT_AS", config.BOT_MEMORY_LIMIT_BYTES, config.BOT_MEMORY_LIMIT_BYTES),
        ("RLIMIT_NOFILE", config.BOT_MAX_OPEN_FILES, config.BOT_MAX_OPEN_FILES),
        ("RLIMIT_NPROC", config.BOT_MAX_PROCESSES, config.BOT_MAX_PROCESSES),
    )
    for limit_name, soft_limit, hard_limit in limits:
        resource_id = getattr(resource, limit_name, None)
        if resource_id is None:
            continue
        try:
            current_soft, current_hard = resource.getrlimit(resource_id)
            applied_soft = min(soft_limit, current_soft) if current_soft >= 0 else soft_limit
            applied_hard = min(hard_limit, current_hard) if current_hard >= 0 else hard_limit
            resource.setrlimit(resource_id, (applied_soft, applied_hard))
        except (OSError, ValueError):
            continue


def _is_valid_return(move: object, valid_moves: list[Move]) -> bool:
    """Chỉ chấp nhận cặp tuple tọa độ nguyên có sẵn trong danh sách hợp lệ."""
    if type(move) is not tuple or len(move) != 2:
        return False
    for coordinate in move:
        if type(coordinate) is not tuple or len(coordinate) != 2:
            return False
        if any(type(value) is not int for value in coordinate):
            return False
    return move in valid_moves