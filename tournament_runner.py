"""Điều phối các giai đoạn chấm giải OTTv2 và tạo dữ liệu replay."""

from __future__ import annotations

import hashlib
import multiprocessing
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import uuid
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence

import config
from benchmark_bots import BotStrategy, standard_bots
from engine import GameResult, OTTGame, Piece
from sandbox import BotSandbox, scan_bot_source


Board = tuple[tuple[Piece | None, ...], ...]
CheckpointCallback = Callable[[str, object], None]
GameExecutor = Callable[["GameTask"], "GameWorkerResult"]


@dataclass(frozen=True, slots=True)
class BotEntrant:
    """Bài nộp và hạt giống xếp hạng của một thí sinh."""

    student_id: str
    source: str | None
    historical_seed: int | None = None
    submission_error: str | None = None


@dataclass(frozen=True, slots=True)
class GameParticipant:
    """Đại diện bot người dùng hoặc bot chuẩn trong một ván."""

    student_id: str
    source: str | None = None
    benchmark_kind: str | None = None
    submission_error: str | None = None


@dataclass(frozen=True, slots=True)
class GameTask:
    """Dữ liệu bất biến cần thiết để worker chạy một ván."""

    run_id: str
    match_id: str
    game_index: int
    p1: GameParticipant
    p2: GameParticipant
    max_plies: int = config.MAX_PLIES


class GameWorkerResult(NamedTuple):
    """Bộ ba worker trả về để lưu kết quả và replay."""

    game_id: str
    moves_bytes: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GatekeeperStanding:
    """Thành tích benchmark và khóa xếp hạng giai đoạn một."""

    student_id: str
    points: int
    average_move_seconds: float
    capture_difference: int
    valid_submission: bool


@dataclass(frozen=True, slots=True)
class SwissStanding:
    """Thành tích Swiss tại một thời điểm."""

    student_id: str
    points: int
    buchholz: int
    capture_difference: int
    seed: int


@dataclass(frozen=True, slots=True)
class SwissPairing:
    """Một cặp đấu Swiss trong một vòng."""

    first_id: str
    second_id: str


@dataclass(frozen=True, slots=True)
class SwissResult:
    """Bảng xếp hạng và lịch sử đối thủ sau Swiss."""

    standings: tuple[SwissStanding, ...]
    opponents: Mapping[str, frozenset[str]]
    game_results: tuple[GameWorkerResult, ...]


@dataclass(frozen=True, slots=True)
class SeriesResult:
    """Kết quả Bo2 có tie-break xác định."""

    winner_id: str
    loser_id: str
    first_points: int
    second_points: int
    first_capture_difference: int
    second_capture_difference: int
    first_moves: int
    second_moves: int
    game_results: tuple[GameWorkerResult, ...]


@dataclass(frozen=True, slots=True)
class DoubleEliminationResult:
    """Thứ tự duy nhất của nhánh loại kép cùng dữ liệu các ván."""

    ranking: tuple[str, ...]
    game_results: tuple[GameWorkerResult, ...]


@dataclass(frozen=True, slots=True)
class TournamentSummary:
    """Kết quả cuối ngày với rank liên tục từ 1 đến số thí sinh."""

    run_id: str
    ranks: Mapping[str, int]
    gatekeeper: tuple[GatekeeperStanding, ...]
    swiss: tuple[SwissStanding, ...]
    playoff_order: tuple[str, ...]
    game_results: tuple[GameWorkerResult, ...]
    partial: bool = False


class PairingError(RuntimeError):
    """Không thể ghép đủ cặp Swiss mà không tái đấu."""


class TournamentDeadlineReached(RuntimeError):
    """Dừng một giai đoạn vì đã tới thời điểm kết thúc chấm."""


def derive_seed(run_id: str, match_id: str) -> int:
    """Tạo seed ổn định giữa các tiến trình và lần chạy Python."""
    digest = hashlib.sha256(f"{run_id}\0{match_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def assert_valid_ranking(
    ranks: Mapping[str, int], expected_count: int | None = None
) -> None:
    """Bảo đảm mỗi thí sinh có đúng một hạng liên tục, không trùng."""
    if len(ranks) != len(set(ranks)):
        raise ValueError("student_id bị trùng trong bảng xếp hạng")
    if expected_count is not None and len(ranks) != expected_count:
        raise ValueError("Số thí sinh trong bảng xếp hạng không đúng")
    expected_ranks = set(range(1, len(ranks) + 1))
    if set(ranks.values()) != expected_ranks:
        raise ValueError("Rank phải là hoán vị duy nhất từ 1 đến số thí sinh")


def play_game(task: GameTask) -> GameWorkerResult:
    """Chạy một ván và mã hóa mỗi ply thành hai byte ô đi/ô đến."""
    game_id = _game_id(task.run_id, task.match_id, task.game_index)
    game = OTTGame(max_plies=task.max_plies)
    move_bytes = bytearray()
    move_time = {1: 0.0, 2: 0.0}
    move_count = {1: 0, 2: 0}
    agents = {1: task.p1, 2: task.p2}
    random_bots = standard_bots(derive_seed(task.run_id, task.match_id))
    sandboxes: dict[int, BotSandbox] = {}
    for player, participant in agents.items():
        if participant.source is not None:
            sandboxes[player] = BotSandbox(
                participant.source,
                seed=derive_seed(task.run_id, task.match_id),
            )

    forfeiting_player: int | None = None
    error_reason = ""
    while not game.is_over():
        player = game.current_player
        participant = agents[player]
        valid_moves = game.get_valid_moves(player)
        move_started = time.perf_counter()

        if participant.benchmark_kind is not None:
            bot: BotStrategy | None = random_bots.get(participant.benchmark_kind)
            if bot is None:
                move = None
                error_reason = "Không tồn tại bot chuẩn"
            else:
                try:
                    move = bot.make_move(game.to_state().board, valid_moves, player)
                except Exception as exc:
                    move = None
                    error_reason = f"{type(exc).__name__}: {exc}"
        elif participant.source is not None:
            result = sandboxes[player].run_move(
                game.to_state().board, valid_moves, player
            )
            move = result.move
            error_reason = result.error or ""
        else:
            move = None
            error_reason = participant.submission_error or "Không có bài nộp hợp lệ"

        move_time[player] += time.perf_counter() - move_started
        if move is None or move not in valid_moves:
            forfeiting_player = player
            if not error_reason:
                error_reason = "Bot trả về nước đi không hợp lệ"
            break

        game.apply_move(move)
        move_count[player] += 1
        from_row, from_col = move[0]
        to_row, to_col = move[1]
        move_bytes.extend(
            (from_row * config.BOARD_SIZE + from_col, to_row * config.BOARD_SIZE + to_col)
        )

    if forfeiting_player is None:
        result = game.result()
        win_reason = game.win_reason()
    else:
        result = "P2" if forfeiting_player == 1 else "P1"
        win_reason = f"P{forfeiting_player} thua do lỗi bot: {error_reason}"

    metadata: dict[str, Any] = {
        "game_id": game_id,
        "run_id": task.run_id,
        "match_id": task.match_id,
        "game_index": task.game_index,
        "p1_id": task.p1.student_id,
        "p2_id": task.p2.student_id,
        "result": result,
        "win_reason": win_reason,
        "plies": game.plies,
        "captured_p1": sum(game.captured_p1),
        "captured_p2": sum(game.captured_p2),
        "time_p1_seconds": move_time[1],
        "time_p2_seconds": move_time[2],
        "moves_p1": move_count[1],
        "moves_p2": move_count[2],
        "forfeiting_player": forfeiting_player,
    }
    return GameWorkerResult(game_id, bytes(move_bytes), metadata)


def _execute_game_task(task: GameTask) -> GameWorkerResult:
    """Worker Pool cho phép chạy sandbox con trong một tiến trình riêng."""
    process = multiprocessing.current_process()
    if process.daemon:
        process.daemon = False
    return play_game(task)


def _game_id(run_id: str, match_id: str, game_index: int) -> str:
    """Sinh UUID ngẫu nhiên không đoán được cho ván replay."""
    del run_id, match_id, game_index
    return str(uuid.uuid4())


def _points_for(result: GameResult, player: int) -> int:
    """Đổi kết quả ván sang thang 3-1-0."""
    if result == "DRAW":
        return 1
    return 3 if result == f"P{player}" else 0


def _capture_difference(result: GameWorkerResult, student_id: str) -> int:
    """Tính quân ăn trừ quân mất của một thí sinh trong một ván."""
    metadata = result.metadata
    if metadata["p1_id"] == student_id:
        return int(metadata["captured_p1"]) - int(metadata["captured_p2"])
    return int(metadata["captured_p2"]) - int(metadata["captured_p1"])


def pair_swiss(
    standings: Sequence[SwissStanding],
    previous_opponents: Mapping[str, set[str] | frozenset[str]],
    first_round: bool = False,
) -> tuple[SwissPairing, ...]:
    """Ghép toàn bộ cặp theo điểm/seed, backtracking để tránh tái đấu."""
    ordered = sorted(standings, key=lambda item: (item.points * -1, item.seed, item.student_id))
    if len(ordered) % 2:
        raise PairingError("Swiss yêu cầu số thí sinh chẵn, không dùng BYE")
    if len({item.student_id for item in ordered}) != len(ordered):
        raise PairingError("student_id bị trùng trong Swiss")
    if first_round:
        half = len(ordered) // 2
        return tuple(
            SwissPairing(ordered[index].student_id, ordered[index + half].student_id)
            for index in range(half)
        )

    by_id = {item.student_id: item for item in ordered}
    ids = tuple(item.student_id for item in ordered)
    played = {
        student_id: set(previous_opponents.get(student_id, set()))
        for student_id in ids
    }

    @lru_cache(maxsize=None)
    def pair_remaining(remaining: tuple[str, ...]) -> tuple[tuple[str, str], ...] | None:
        if not remaining:
            return ()
        first_id = remaining[0]
        candidates = [
            other_id
            for other_id in remaining[1:]
            if other_id not in played[first_id] and first_id not in played[other_id]
        ]
        if not candidates:
            return None
        midpoint = len(remaining) / 2
        candidates.sort(
            key=lambda other_id: (
                abs(by_id[first_id].points - by_id[other_id].points),
                0 if remaining.index(other_id) >= midpoint else 1,
                abs(by_id[first_id].seed - by_id[other_id].seed),
                by_id[other_id].seed,
                other_id,
            )
        )
        for other_id in candidates:
            next_remaining = tuple(
                student_id
                for student_id in remaining[1:]
                if student_id != other_id
            )
            rest = pair_remaining(next_remaining)
            if rest is not None:
                return ((first_id, other_id),) + rest
        return None

    pairs = pair_remaining(ids)
    if pairs is None:
        raise PairingError("Không thể ghép Swiss mà không tái đấu")
    return tuple(SwissPairing(first_id, second_id) for first_id, second_id in pairs)


class TournamentRunner:
    """Chạy ba giai đoạn, thu replay và phát checkpoint cho bên lưu trữ."""

    def __init__(
        self,
        run_id: str,
        workers: int = config.TOURNAMENT_WORKERS,
        game_executor: GameExecutor | None = None,
        checkpoint: CheckpointCallback | None = None,
        deadline_monotonic: float | None = None,
        parallel_game_executor: bool = False,
    ) -> None:
        if workers < 1:
            raise ValueError("workers phải lớn hơn hoặc bằng 1")
        self.run_id = run_id
        self.workers = workers
        self.game_executor = game_executor
        self.parallel_game_executor = parallel_game_executor
        self.checkpoint = checkpoint
        self.deadline_monotonic = deadline_monotonic
        self.partial = False
        self.game_results: list[GameWorkerResult] = []

    def _run_games(self, tasks: Iterable[GameTask]) -> list[GameWorkerResult]:
        results: list[GameWorkerResult] = []
        if self.game_executor is not None:
            if self.parallel_game_executor and self.workers > 1:
                return self._run_games_parallel(tasks)
            for task in tasks:
                if self._deadline_expired():
                    self.partial = True
                    break
                results.append(self.game_executor(task))
        elif self.workers == 1:
            for task in tasks:
                if self._deadline_expired():
                    self.partial = True
                    break
                results.append(_execute_game_task(task))
        else:
            pool = multiprocessing.Pool(processes=self.workers)
            iterator = pool.imap_unordered(_execute_game_task, tasks, chunksize=1)
            try:
                while True:
                    remaining = self._deadline_remaining()
                    if remaining is not None and remaining <= 0:
                        self.partial = True
                        pool.terminate()
                        break
                    wait_seconds = 0.1 if remaining is None else min(0.1, remaining)
                    try:
                        results.append(iterator.next(timeout=wait_seconds))
                    except multiprocessing.TimeoutError:
                        continue
                    except StopIteration:
                        pool.close()
                        break
            except BaseException:
                pool.terminate()
                raise
            finally:
                pool.join()
        self.game_results.extend(results)
        return results

    def _run_games_parallel(self, tasks: Iterable[GameTask]) -> list[GameWorkerResult]:
        """Bound concurrent remote game jobs and honor the scoring deadline."""
        if self.game_executor is None:
            raise RuntimeError("Parallel execution requires a game executor")
        results: list[GameWorkerResult] = []
        task_iterator = iter(tasks)
        pending: set[Future[GameWorkerResult]] = set()
        exhausted = False
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while pending or not exhausted:
                while len(pending) < self.workers and not exhausted:
                    if self._deadline_expired():
                        self.partial = True
                        exhausted = True
                        break
                    try:
                        task = next(task_iterator)
                    except StopIteration:
                        exhausted = True
                    else:
                        pending.add(pool.submit(self.game_executor, task))
                if not pending:
                    break
                remaining = self._deadline_remaining()
                completed, pending = wait(
                    pending,
                    timeout=remaining,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    self.partial = True
                    for future in pending:
                        future.cancel()
                    break
                results.extend(future.result() for future in completed)
        self.game_results.extend(results)
        return results

    def run_gatekeeper(
        self,
        entrants: Sequence[BotEntrant],
        cutoff: int = config.GATEKEEPER_CUTOFF,
    ) -> tuple[GatekeeperStanding, ...]:
        """Chạy sáu ván mỗi bài hợp lệ và xếp thứ tự loại benchmark."""
        if cutoff < 1 or cutoff > len(entrants):
            raise ValueError("cutoff phải nằm trong số thí sinh")
        self._validate_entrants(entrants)
        valid_by_id = {
            entrant.student_id: self._is_valid_submission(entrant)
            for entrant in entrants
        }
        participants = {
            entrant.student_id: GameParticipant(
                entrant.student_id,
                source=entrant.source,
                submission_error=entrant.submission_error,
            )
            for entrant in entrants
        }

        def tasks() -> Iterable[GameTask]:
            benchmark_kinds = ("RandomBot", "GreedyBot", "RushBot")
            if config.GATEKEEPER_GAMES_PER_BOT % len(benchmark_kinds):
                raise ValueError("Số ván gatekeeper phải chia hết cho số bot chuẩn")
            games_per_side = config.GATEKEEPER_GAMES_PER_BOT // len(benchmark_kinds)
            for entrant in entrants:
                if not valid_by_id[entrant.student_id]:
                    continue
                for benchmark_kind in benchmark_kinds:
                    baseline = GameParticipant(
                        f"@{benchmark_kind}", benchmark_kind=benchmark_kind
                    )
                    for leg in range(games_per_side):
                        p1, p2 = (
                            (participants[entrant.student_id], baseline)
                            if leg == 0
                            else (baseline, participants[entrant.student_id])
                        )
                        match_id = f"gatekeeper:{entrant.student_id}:{benchmark_kind}:{leg}"
                        yield GameTask(self.run_id, match_id, 0, p1, p2)

        results = self._run_games(tasks())
        totals: dict[str, dict[str, float | int]] = {
            entrant.student_id: {
                "points": 0,
                "capture_difference": 0,
                "time_seconds": 0.0,
                "moves": 0,
            }
            for entrant in entrants
        }
        for result in results:
            metadata = result.metadata
            for player, student_id in ((1, metadata["p1_id"]), (2, metadata["p2_id"])):
                if student_id not in totals:
                    continue
                totals[student_id]["points"] = int(totals[student_id]["points"]) + _points_for(
                    metadata["result"], player
                )
                totals[student_id]["capture_difference"] = int(
                    totals[student_id]["capture_difference"]
                ) + _capture_difference(result, student_id)
                totals[student_id]["time_seconds"] = float(
                    totals[student_id]["time_seconds"]
                ) + float(metadata[f"time_p{player}_seconds"])
                totals[student_id]["moves"] = int(totals[student_id]["moves"]) + int(
                    metadata[f"moves_p{player}"]
                )

        standings = [
            GatekeeperStanding(
                student_id=entrant.student_id,
                points=int(totals[entrant.student_id]["points"]),
                average_move_seconds=(
                    float(totals[entrant.student_id]["time_seconds"])
                    / max(1, int(totals[entrant.student_id]["moves"]))
                ),
                capture_difference=int(totals[entrant.student_id]["capture_difference"]),
                valid_submission=valid_by_id[entrant.student_id],
            )
            for entrant in entrants
        ]
        standings.sort(
            key=lambda item: (
                not item.valid_submission,
                -item.points,
                item.average_move_seconds,
                -item.capture_difference,
                item.student_id,
            )
        )
        result = tuple(standings)
        self._checkpoint("gatekeeper", result)
        return result

    def run_swiss(
        self,
        entrants: Sequence[BotEntrant],
        seed_order: Sequence[str],
        rounds: int = config.SWISS_ROUNDS,
    ) -> SwissResult:
        """Chạy Swiss Bo2, cập nhật điểm, Buchholz và hiệu số quân ăn."""
        if rounds < 1:
            raise ValueError("Swiss cần ít nhất một vòng")
        if len(entrants) < 2 or len(entrants) % 2:
            raise ValueError("Swiss cần số thí sinh chẵn và ít nhất hai người")
        self._validate_entrants(entrants)
        entrant_by_id = {entrant.student_id: entrant for entrant in entrants}
        if set(seed_order) != set(entrant_by_id) or len(seed_order) != len(entrants):
            raise ValueError("seed_order phải chứa mỗi thí sinh đúng một lần")
        seed_by_id = {
            student_id: index + 1 for index, student_id in enumerate(seed_order)
        }
        points = {student_id: 0 for student_id in seed_order}
        capture_diff = {student_id: 0 for student_id in seed_order}
        opponents: dict[str, set[str]] = {student_id: set() for student_id in seed_order}
        all_results: list[GameWorkerResult] = []

        for round_no in range(1, rounds + 1):
            current = self._swiss_standings(points, capture_diff, seed_by_id, opponents)
            pairings = pair_swiss(
                current,
                opponents,
                first_round=round_no == 1,
            )
            round_tasks = (
                task
                for pairing in pairings
                for task in self._swiss_tasks(
                    entrant_by_id[pairing.first_id],
                    entrant_by_id[pairing.second_id],
                    round_no,
                )
            )
            round_results = self._run_games(round_tasks)
            all_results.extend(round_results)
            grouped: dict[str, list[GameWorkerResult]] = defaultdict(list)
            for result in round_results:
                grouped[str(result.metadata["match_id"])].append(result)
            for pairing in pairings:
                opponents[pairing.first_id].add(pairing.second_id)
                opponents[pairing.second_id].add(pairing.first_id)
                match_id = f"swiss:{round_no}:{pairing.first_id}:{pairing.second_id}"
                completed_match = grouped[match_id]
                if len(completed_match) != config.SWISS_GAMES_PER_MATCH:
                    opponents[pairing.first_id].discard(pairing.second_id)
                    opponents[pairing.second_id].discard(pairing.first_id)
                    continue
                for game_result in completed_match:
                    metadata = game_result.metadata
                    points[pairing.first_id] += _points_for(
                        metadata["result"], 1 if metadata["p1_id"] == pairing.first_id else 2
                    )
                    points[pairing.second_id] += _points_for(
                        metadata["result"], 1 if metadata["p1_id"] == pairing.second_id else 2
                    )
                    capture_diff[pairing.first_id] += _capture_difference(
                        game_result, pairing.first_id
                    )
                    capture_diff[pairing.second_id] += _capture_difference(
                        game_result, pairing.second_id
                    )
            self._checkpoint(
                f"swiss_round_{round_no}",
                {"standings": self._swiss_standings(points, capture_diff, seed_by_id, opponents),
                 "opponents": {key: frozenset(value) for key, value in opponents.items()}},
            )
            if self.partial:
                break

        final_standings = self._swiss_standings(points, capture_diff, seed_by_id, opponents)
        final_standings.sort(
            key=lambda item: (
                -item.points,
                -item.buchholz,
                -item.capture_difference,
                item.seed,
                item.student_id,
            )
        )
        return SwissResult(
            tuple(final_standings),
            {key: frozenset(value) for key, value in opponents.items()},
            tuple(all_results),
        )

    def run_double_elimination(
        self,
        entrants: Sequence[BotEntrant],
        seed_order: Sequence[str],
        reset_final: bool = config.DOUBLE_ELIMINATION_RESET,
    ) -> DoubleEliminationResult:
        """Chạy nhánh thắng/thua và trả thứ tự loại duy nhất."""
        if len(entrants) < 2:
            raise ValueError("Nhánh loại kép cần ít nhất hai thí sinh")
        if len(entrants) & (len(entrants) - 1):
            raise ValueError("Số thí sinh nhánh loại kép phải là lũy thừa của 2")
        self._validate_entrants(entrants)
        entrant_by_id = {entrant.student_id: entrant for entrant in entrants}
        if set(seed_order) != set(entrant_by_id) or len(seed_order) != len(entrants):
            raise ValueError("seed_order phải chứa mỗi thí sinh đúng một lần")
        seed_by_id = {student_id: index + 1 for index, student_id in enumerate(seed_order)}
        undefeated = list(seed_order)
        one_loss: list[str] = []
        eliminated: list[tuple[int, int, str]] = []
        round_no = 0
        playoff_results: list[GameWorkerResult] = []

        while len(undefeated) > 1:
            round_no += 1
            undefeated.sort(key=lambda student_id: seed_by_id[student_id])
            winner_pairs = _bracket_pairs(undefeated)
            winner_results = self._play_series_batch(
                winner_pairs, entrant_by_id, f"playoff:winners:{round_no}"
            )
            playoff_results.extend(
                result for series in winner_results for result in series.game_results
            )
            undefeated = [series.winner_id for series in winner_results]
            for series in winner_results:
                one_loss.append(series.loser_id)

            if len(one_loss) > 1:
                one_loss.sort(key=lambda student_id: seed_by_id[student_id])
                lower_pairs, bye = _lower_bracket_pairs(one_loss)
                lower_results = self._play_series_batch(
                    lower_pairs, entrant_by_id, f"playoff:losers:{round_no}"
                )
                playoff_results.extend(
                    result for series in lower_results for result in series.game_results
                )
                remaining = [series.winner_id for series in lower_results]
                if bye is not None:
                    remaining.append(bye)
                for series in lower_results:
                    eliminated.append((round_no, seed_by_id[series.loser_id], series.loser_id))
                one_loss = remaining
            self._checkpoint(
                f"playoff_round_{round_no}",
                {"undefeated": tuple(undefeated), "one_loss": tuple(one_loss),
                 "eliminated": tuple(item[2] for item in eliminated)},
            )

        while len(one_loss) > 1:
            round_no += 1
            one_loss.sort(key=lambda student_id: seed_by_id[student_id])
            lower_pairs, bye = _lower_bracket_pairs(one_loss)
            lower_results = self._play_series_batch(
                lower_pairs, entrant_by_id, f"playoff:losers:{round_no}"
            )
            playoff_results.extend(
                result for series in lower_results for result in series.game_results
            )
            one_loss = [series.winner_id for series in lower_results]
            if bye is not None:
                one_loss.append(bye)
            for series in lower_results:
                eliminated.append((round_no, seed_by_id[series.loser_id], series.loser_id))

        upper_finalist = undefeated[0]
        lower_finalist = one_loss[0]
        final = self._play_series(
            entrant_by_id[upper_finalist],
            entrant_by_id[lower_finalist],
            f"playoff:grand_final:{round_no + 1}",
            seed_by_id,
        )
        playoff_results.extend(final.game_results)
        champion = final.winner_id
        runner_up = final.loser_id
        if reset_final and champion == lower_finalist:
            reset = self._play_series(
                entrant_by_id[upper_finalist],
                entrant_by_id[lower_finalist],
                f"playoff:grand_final_reset:{round_no + 2}",
                seed_by_id,
            )
            playoff_results.extend(reset.game_results)
            champion, runner_up = reset.winner_id, reset.loser_id

        eliminated.sort(key=lambda item: (item[0] * -1, item[1], item[2]))
        ranking = (champion, runner_up, *(item[2] for item in eliminated))
        if len(ranking) != len(entrants) or len(set(ranking)) != len(entrants):
            raise RuntimeError("Nhánh loại kép không tạo được thứ hạng duy nhất")
        result = DoubleEliminationResult(tuple(ranking), tuple(playoff_results))
        self._checkpoint("playoff_complete", result)
        return result

    def run(
        self,
        entrants: Sequence[BotEntrant],
        gatekeeper_cutoff: int = config.GATEKEEPER_CUTOFF,
        swiss_rounds: int = config.SWISS_ROUNDS,
        playoff_cutoff: int = config.PLAYOFF_CUTOFF,
        reset_final: bool = config.DOUBLE_ELIMINATION_RESET,
    ) -> TournamentSummary:
        """Chạy ba giai đoạn và gán rank cuối ngày 1..N."""
        self._validate_entrants(entrants)
        if gatekeeper_cutoff > len(entrants) or gatekeeper_cutoff < 2:
            raise ValueError("gatekeeper_cutoff phải từ 2 đến số thí sinh")
        if gatekeeper_cutoff % 2:
            raise ValueError("gatekeeper_cutoff phải là số chẵn")
        if playoff_cutoff < 2 or playoff_cutoff > gatekeeper_cutoff:
            raise ValueError("playoff_cutoff phải từ 2 đến gatekeeper_cutoff")

        gatekeeper = self.run_gatekeeper(entrants, gatekeeper_cutoff)
        if self.partial:
            final_order = [item.student_id for item in gatekeeper]
            summary = self._partial_summary(final_order, gatekeeper, (), ())
            self._checkpoint("partial", summary)
            return summary
        qualified = [item.student_id for item in gatekeeper[:gatekeeper_cutoff]]
        entrant_by_id = {entrant.student_id: entrant for entrant in entrants}
        qualified_entrants = [entrant_by_id[student_id] for student_id in qualified]
        gatekeeper_seed = {
            item.student_id: index + 1 for index, item in enumerate(gatekeeper)
        }
        swiss_seed_order = sorted(
            qualified,
            key=lambda student_id: (
                entrant_by_id[student_id].historical_seed is None,
                entrant_by_id[student_id].historical_seed
                if entrant_by_id[student_id].historical_seed is not None
                else gatekeeper_seed[student_id],
                gatekeeper_seed[student_id],
                student_id,
            ),
        )
        swiss_result = self.run_swiss(
            qualified_entrants, swiss_seed_order, swiss_rounds
        )
        swiss_order = [item.student_id for item in swiss_result.standings]
        if self.partial:
            final_order = [
                *swiss_order,
                *(item.student_id for item in gatekeeper[gatekeeper_cutoff:]),
            ]
            summary = self._partial_summary(
                final_order, gatekeeper, swiss_result.standings, ()
            )
            self._checkpoint("partial", summary)
            return summary
        playoff_ids = swiss_order[:playoff_cutoff]
        playoff_entrants = [entrant_by_id[student_id] for student_id in playoff_ids]
        try:
            playoff = self.run_double_elimination(
                playoff_entrants, playoff_ids, reset_final=reset_final
            )
        except TournamentDeadlineReached:
            final_order = [
                *swiss_order,
                *(item.student_id for item in gatekeeper[gatekeeper_cutoff:]),
            ]
            summary = self._partial_summary(
                final_order, gatekeeper, swiss_result.standings, ()
            )
            self._checkpoint("partial", summary)
            return summary

        final_order = [
            *playoff.ranking,
            *swiss_order[playoff_cutoff:],
            *(item.student_id for item in gatekeeper[gatekeeper_cutoff:]),
        ]
        ranks = {student_id: rank for rank, student_id in enumerate(final_order, start=1)}
        assert_valid_ranking(ranks, expected_count=len(entrants))
        summary = TournamentSummary(
            self.run_id,
            ranks,
            gatekeeper,
            swiss_result.standings,
            playoff.ranking,
            tuple(self.game_results),
        )
        self._checkpoint("complete", summary)
        return summary

    def _partial_summary(
        self,
        order: Sequence[str],
        gatekeeper: Sequence[GatekeeperStanding],
        swiss: Sequence[SwissStanding],
        playoff_order: Sequence[str],
    ) -> TournamentSummary:
        """Tạo rank tạm liên tục từ phần dữ liệu đã hoàn tất."""
        ranks = {student_id: rank for rank, student_id in enumerate(order, start=1)}
        assert_valid_ranking(ranks)
        return TournamentSummary(
            self.run_id,
            ranks,
            tuple(gatekeeper),
            tuple(swiss),
            tuple(playoff_order),
            tuple(self.game_results),
            partial=True,
        )

    def _deadline_remaining(self) -> float | None:
        """Trả thời gian còn lại theo đồng hồ monotonic, nếu có deadline."""
        if self.deadline_monotonic is None:
            return None
        return self.deadline_monotonic - time.monotonic()

    def _deadline_expired(self) -> bool:
        """Kiểm tra deadline trước khi bắt đầu game tiếp theo."""
        remaining = self._deadline_remaining()
        return remaining is not None and remaining <= 0

    def _swiss_tasks(
        self,
        first: BotEntrant,
        second: BotEntrant,
        round_no: int,
    ) -> tuple[GameTask, GameTask]:
        """Tạo hai ván đổi bên cho một trận Swiss."""
        match_id = f"swiss:{round_no}:{first.student_id}:{second.student_id}"
        first_player = _participant(first)
        second_player = _participant(second)
        return tuple(
            GameTask(
                self.run_id,
                match_id,
                game_index,
                first_player if game_index % 2 == 0 else second_player,
                second_player if game_index % 2 == 0 else first_player,
            )
            for game_index in range(config.SWISS_GAMES_PER_MATCH)
        )

    def _play_series_batch(
        self,
        pairs: Sequence[tuple[str, str]],
        entrants: Mapping[str, BotEntrant],
        match_prefix: str,
    ) -> list[SeriesResult]:
        """Chạy đồng thời từng cặp Bo2 trong cùng một lượt nhánh."""
        series_tasks: list[tuple[str, str, str, tuple[GameTask, GameTask]]] = []
        flat_tasks: list[GameTask] = []
        for index, (first_id, second_id) in enumerate(pairs, start=1):
            match_id = f"{match_prefix}:match:{index}"
            first = _participant(entrants[first_id])
            second = _participant(entrants[second_id])
            tasks = tuple(
                GameTask(
                    self.run_id,
                    match_id,
                    game_index,
                    first if game_index % 2 == 0 else second,
                    second if game_index % 2 == 0 else first,
                )
                for game_index in range(config.SWISS_GAMES_PER_MATCH)
            )
            series_tasks.append((first_id, second_id, match_id, tasks))
            flat_tasks.extend(tasks)
        results = self._run_games(flat_tasks)
        if self.partial:
            raise TournamentDeadlineReached("Đã tới thời điểm dừng chấm")
        by_match: dict[str, list[GameWorkerResult]] = defaultdict(list)
        for result in results:
            by_match[str(result.metadata["match_id"])].append(result)
        output = []
        for first_id, second_id, match_id, _ in series_tasks:
            games = sorted(by_match[match_id], key=lambda item: item.metadata["game_index"])
            output.append(_decide_series(first_id, second_id, games, _seed_order_from_pairs(pairs)))
        return output

    def _play_series(
        self,
        first: BotEntrant,
        second: BotEntrant,
        match_id: str,
        seed_by_id: Mapping[str, int] | None = None,
    ) -> SeriesResult:
        """Chạy một Bo2 đổi bên và áp dụng đầy đủ tie-break."""
        first_participant = _participant(first)
        second_participant = _participant(second)
        tasks = tuple(
            GameTask(
                self.run_id,
                match_id,
                game_index,
                first_participant if game_index % 2 == 0 else second_participant,
                second_participant if game_index % 2 == 0 else first_participant,
            )
            for game_index in range(config.SWISS_GAMES_PER_MATCH)
        )
        games = sorted(
            self._run_games(tasks), key=lambda item: item.metadata["game_index"]
        )
        if self.partial:
            raise TournamentDeadlineReached("Đã tới thời điểm dừng chấm")
        seed_order = seed_by_id or {
            first.student_id: first.historical_seed or 1,
            second.student_id: second.historical_seed or 2,
        }
        return _decide_series(first.student_id, second.student_id, games, seed_order)

    def _swiss_standings(
        self,
        points: Mapping[str, int],
        capture_diff: Mapping[str, int],
        seed_by_id: Mapping[str, int],
        opponents: Mapping[str, set[str]],
    ) -> list[SwissStanding]:
        """Tạo standings kèm Buchholz hiện tại."""
        return [
            SwissStanding(
                student_id,
                points[student_id],
                sum(points[opponent] for opponent in opponents[student_id]),
                capture_diff[student_id],
                seed_by_id[student_id],
            )
            for student_id in points
        ]

    def _is_valid_submission(self, entrant: BotEntrant) -> bool:
        """Kiểm tra bài tồn tại, không có lỗi đã biết và qua AST scan."""
        return (
            entrant.source is not None
            and entrant.submission_error is None
            and scan_bot_source(entrant.source).valid
        )

    def _validate_entrants(self, entrants: Sequence[BotEntrant]) -> None:
        """Từ chối danh sách rỗng hoặc mã thí sinh lặp."""
        student_ids = [entrant.student_id for entrant in entrants]
        if not student_ids:
            raise ValueError("Danh sách thí sinh không được rỗng")
        if len(set(student_ids)) != len(student_ids):
            raise ValueError("student_id bị trùng")

    def _checkpoint(self, stage: str, state: object) -> None:
        """Gửi trạng thái bất biến cho lớp lưu checkpoint bên ngoài."""
        if self.checkpoint is not None:
            self.checkpoint(stage, state)


def _participant(entrant: BotEntrant) -> GameParticipant:
    """Chuyển bài nộp thành đối tượng dùng trong ván."""
    return GameParticipant(
        entrant.student_id,
        source=entrant.source,
        submission_error=entrant.submission_error,
    )


def _bracket_pairs(entrants: Sequence[str]) -> list[tuple[str, str]]:
    """Ghép hạt giống đầu với cuối của nhánh thắng."""
    return [(entrants[index], entrants[-index - 1]) for index in range(len(entrants) // 2)]


def _lower_bracket_pairs(
    entrants: Sequence[str],
) -> tuple[list[tuple[str, str]], str | None]:
    """Ghép nhánh thua; thí sinh hạt giống tốt nhất được BYE khi lẻ."""
    ordered = list(entrants)
    bye = ordered.pop(0) if len(ordered) % 2 else None
    pairs = [(ordered[index], ordered[-index - 1]) for index in range(len(ordered) // 2)]
    return pairs, bye


def _seed_order_from_pairs(pairs: Sequence[tuple[str, str]]) -> dict[str, int]:
    """Tạo hạt giống cục bộ theo thứ tự xuất hiện trong cặp."""
    return {
        student_id: index + 1
        for index, student_id in enumerate(
            dict.fromkeys(student_id for pair in pairs for student_id in pair)
        )
    }


def _decide_series(
    first_id: str,
    second_id: str,
    games: Sequence[GameWorkerResult],
    seed_by_id: Mapping[str, int],
) -> SeriesResult:
    """Phân định Bo2 theo điểm, hiệu số quân, số nước, rồi hạt giống."""
    if len(games) != config.SWISS_GAMES_PER_MATCH:
        raise ValueError("Series Bo2 phải có đúng hai ván")
    points = {first_id: 0, second_id: 0}
    captures = {first_id: 0, second_id: 0}
    move_counts = {first_id: 0, second_id: 0}
    for game_result in games:
        metadata = game_result.metadata
        for player, student_id in ((1, metadata["p1_id"]), (2, metadata["p2_id"])):
            if student_id not in points:
                raise ValueError("Ván trong series có thí sinh không khớp")
            points[student_id] += _points_for(metadata["result"], player)
            captures[student_id] += _capture_difference(game_result, student_id)
            move_counts[student_id] += int(metadata[f"moves_p{player}"])

    first_key = (
        points[first_id],
        captures[first_id],
        -move_counts[first_id],
        -seed_by_id[first_id],
    )
    second_key = (
        points[second_id],
        captures[second_id],
        -move_counts[second_id],
        -seed_by_id[second_id],
    )
    winner_id, loser_id = (
        (first_id, second_id) if first_key >= second_key else (second_id, first_id)
    )
    return SeriesResult(
        winner_id,
        loser_id,
        points[first_id],
        points[second_id],
        captures[first_id],
        captures[second_id],
        move_counts[first_id],
        move_counts[second_id],
        tuple(games),
    )