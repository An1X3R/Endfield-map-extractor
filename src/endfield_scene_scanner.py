"""Run independent scanner processes and publish only fully verified ordered scans."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
from queue import SimpleQueue
from threading import Event
import time

from endfield_scene_capacity import available_physical_memory_bytes, system_memory_snapshot
from endfield_scene_pipeline import ScenePipelineCancelled, SceneWorkerCleanupError
from endfield_scene_resources import read_json, write_index


Emit = Callable[[dict[str, object]], None]
Observe = Callable[[list[str], Path, Path, Callable[[], None], Emit], None]


@dataclass(frozen=True)
class ScanShard:
    start: int
    end: int


@dataclass(frozen=True)
class ScannerCapacity:
    workers: int
    worker_budget_bytes: int
    reserve_bytes: int
    available_bytes: int


def scan_index_rows(path: Path, check_cancelled: Callable[[], None]) -> int:
    """Count fixed index rows without retaining the index in memory."""
    with path.open(encoding="utf-8-sig") as stream:
        if stream.readline().rstrip("\r\n") != "logical_name\tchunk_path\toffset\tlength":
            raise ValueError(f"Unexpected scanner index header: {path}")
        count = 0
        for count, _ in enumerate(stream, 1):
            if count % 4096 == 0:
                check_cancelled()
    check_cancelled()
    return count


def scanner_capacity(rows: int) -> ScannerCapacity:
    """Bound independent processes by CPUs and physical/commit headroom."""
    requested = int(os.environ.get("ENDFIELD_SCANNER_WORKERS", "0"))
    budget_mib = int(os.environ.get("ENDFIELD_SCANNER_MEMORY_MIB", "1024"))
    if requested not in range(5) or budget_mib < 256:
        raise ValueError("ENDFIELD_SCANNER_WORKERS requires 0..4; ENDFIELD_SCANNER_MEMORY_MIB requires >=256")
    if os.name == "nt":
        system = system_memory_snapshot()
        available = min(system.available_physical_bytes, system.available_commit_bytes)
    else:
        available = available_physical_memory_bytes()
    budget, reserve = budget_mib * 1024**2, 1024**3
    memory_workers = (available - reserve) // budget
    if rows and memory_workers < 1:
        raise MemoryError(f"Scanner memory admission failed: available={available}, reserve={reserve}, worker_budget={budget}")
    cpu_workers = max(1, (os.cpu_count() or 1) // 2)
    workers = min(requested or min(4, cpu_workers), max(1, memory_workers),
                  max(1, rows if requested else rows // 512))
    return ScannerCapacity(workers, budget, reserve, available)


def scan_shards(start: int, end: int, workers: int) -> tuple[ScanShard, ...]:
    """Preserve absolute --skip/--limit semantics with consecutive nonempty ranges."""
    if start < 0 or end < start or workers not in range(1, 5):
        raise ValueError(f"Invalid scanner range/capacity: start={start}, end={end}, workers={workers}")
    count = min(workers, end - start)
    return tuple(ScanShard(start + (end - start) * index // count,
                           start + (end - start) * (index + 1) // count) for index in range(count))


def _complete(path: Path, shard: ScanShard, check_cancelled: Callable[[], None]) -> int:
    """Require one terminal completion with exact range coverage and no errors."""
    last: dict[str, object] = {}
    complete = False
    payloads = 0
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if number % 4096 == 0:
                check_cancelled()
            value = json.loads(line)
            if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or complete:
                raise ValueError(f"Invalid scanner record or data after completion: path={path}, line={number}")
            last = value
            if value["kind"] == "error":
                raise ValueError(f"Scanner bundle failed: path={path}, record={value}")
            complete = value["kind"] == "complete"
            payloads += value["kind"] not in ("progress", "complete")
    if (not complete or type(last.get("failed")) is not int or last["failed"] != 0
            or type(last.get("scanned")) is not int or last["scanned"] != shard.end
            or type(last.get("matches")) is not int or last["matches"] != payloads):
        raise ValueError(f"Scanner completion/range mismatch: path={path}, expected_end={shard.end}, payloads={payloads}, last={last}")
    check_cancelled()
    return payloads


def _merge(paths: tuple[Path, ...], shards: tuple[ScanShard, ...], target: Path,
           check_cancelled: Callable[[], None]) -> None:
    matches = sum(_complete(path, shard, check_cancelled) for path, shard in zip(paths, shards, strict=True))
    with target.open("x", encoding="utf-8", newline="\n") as output:
        for path in paths:
            with path.open(encoding="utf-8-sig") as stream:
                for number, line in enumerate(stream, 1):
                    if number % 4096 == 0:
                        check_cancelled()
                    if json.loads(line)["kind"] not in ("progress", "complete"):
                        output.write(line.rstrip("\r\n") + "\n")
        output.write(json.dumps({"kind": "complete", "scanned": shards[-1].end if shards else 0,
                                 "failed": 0, "matches": matches}) + "\n")
    check_cancelled()


def _read_flushed_errors(path: Path, position: int, fragment: bytes,
                         shard: ScanShard) -> tuple[int, bytes]:
    """Read newly flushed bytes once and preserve the complete original error row."""
    if not path.is_file():
        return position, fragment
    with path.open("rb") as stream:
        stream.seek(position)
        lines = (fragment + stream.read()).split(b"\n")
        next_position = stream.tell()
    pending = lines.pop()
    for line in lines:
        if line.startswith(b'{"kind":"error"'):
            row = json.loads(line)
            raise ValueError(f"Scanner bundle failed: shard=[{shard.start},{shard.end}), path={path}, source_record={row}")
    return next_position, pending


def run_scanner_shards(scanner: Path, index: Path, work: Path, log: Path,
                       start: int, end: int, capacity: ScannerCapacity,
                       container_prefixes: tuple[str, ...],
                       check_cancelled: Callable[[], None], emit: Emit, observe: Observe) -> None:
    """Join/cancel all children before publishing an ordered provider/CAB pair."""
    if (not container_prefixes or len(set(container_prefixes)) != len(container_prefixes)
            or any(not prefix or prefix.startswith("/") or not prefix.endswith("/") or "\\" in prefix
                   for prefix in container_prefixes)):
        raise ValueError(f"Scanner container prefixes must be unique relative slash-terminated paths: {container_prefixes}")
    targets = (work / "bundle_candidates.jsonl", work / "bundle_cab_inventory.jsonl", work / "performance.json")
    if any(path.exists() for path in targets) or log.exists() or (work / "shards").exists():
        raise FileExistsError(f"Scanner requires a fresh work/log destination: work={work}, log={log}")
    shards = scan_shards(start, end, capacity.workers)
    work.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    (work / "shards").mkdir()
    queue: SimpleQueue[tuple[int, dict[str, object]]] = SimpleQueue()
    stopped = Event()
    started = time.perf_counter()
    latest: dict[tuple[int, str], int] = {}
    metrics: dict[int, dict[str, int]] = {}
    events: list[dict[str, object]] = []

    def worker_check() -> None:
        if stopped.is_set():
            raise ScenePipelineCancelled("Scanner stopped after coordinator cancellation or shard failure")

    def execute(number: int, shard: ScanShard) -> Path:
        directory = work / "shards" / f"{number:02d}"
        directory.mkdir()
        command = [str(scanner), "--input", str(index), "--output", str(directory / "providers.jsonl"),
                   "--cab-output", str(directory / "cabs.jsonl")]
        for prefix in container_prefixes:
            command.extend(("--contains", prefix))
        command.extend(("--skip", str(shard.start), "--limit", str(shard.end), "--progress-every", "256",
                        "--performance-report", str(directory / "performance.json")))
        position, fragment = 0, b""

        def check_shard() -> None:
            nonlocal position, fragment
            worker_check()
            position, fragment = _read_flushed_errors(directory / "providers.jsonl", position, fragment, shard)

        observe(command, directory / "console.log", directory / "temp", check_shard,
                lambda row: queue.put((number, row)))
        for name in ("providers.jsonl", "cabs.jsonl"):
            _complete(directory / name, shard, worker_check)
        report = read_json(directory / "performance.json")
        if report.get("format") != "EndfieldHelperPerformanceReport/1" or report.get("status") != "complete":
            raise ValueError(f"Scanner performance receipt is incomplete: {directory}")
        return directory

    def drain() -> None:
        while not queue.empty():
            number, row = queue.get()
            phase = str(row["phase"])
            if phase == "manifest_write":
                continue
            current = row.get("current")
            if type(current) is not int or not 0 <= current <= shards[number].end - shards[number].start:
                raise ValueError(f"Scanner shard progress outside range: shard={number}, row={row}")
            latest[number, phase] = current
            metrics[number] = {key: int(row[key]) for key in ("peakWorkingSetBytes", "readBytes", "writeBytes") if key in row}
            sample: dict[str, object] = {"format": "EndfieldHelperProgress/1", "helper": "EndfieldBundleScannerShards", "phase": phase,
                  "current": sum(max((latest.get((index, name), 0) for name in ("read", "load_unpack", "object_parse")))
                                 for index in range(len(shards))), "total": end - start,
                  "elapsedSeconds": time.perf_counter() - started,
                  **{key: sum(value.get(key, 0) for value in metrics.values()) for key in ("peakWorkingSetBytes", "readBytes", "writeBytes")},
                  "workers": len(shards), "peakSemantics": "sum of child peaks; conservative bound, not simultaneous resident peak",
                  "currentSemantics": "sum of most advanced read/load/parse counts; not phase-specific completion"}
            events.append(sample)
            emit(sample)

    executor = ThreadPoolExecutor(max_workers=capacity.workers, thread_name_prefix="endfield-scan")
    pending: dict[Future[Path], int] = {}
    results: dict[int, Path] = {}
    failure: BaseException | None = None
    try:
        check_cancelled()
        pending = {executor.submit(execute, number, shard): number for number, shard in enumerate(shards)}
        while pending:
            check_cancelled()
            drain()
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=pending.__getitem__):
                results[pending.pop(future)] = future.result()
        drain()
    except BaseException as error:
        failure = error
        raise
    finally:
        stopped.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        cleanup_errors = [error for future in pending if not future.cancelled()
                          and isinstance(error := future.exception(), SceneWorkerCleanupError) and error is not failure]
        if cleanup_errors:
            raise BaseExceptionGroup("Scanner child cleanup failed", ([failure] if failure else []) + cleanup_errors)
    directories = tuple(results[number] for number in range(len(shards)))
    staging = tuple(work / (target.name + ".pending") for target in targets)
    try:
        for name, target in zip(("providers.jsonl", "cabs.jsonl"), staging[:2], strict=True):
            _merge(tuple(directory / name for directory in directories), shards, target, check_cancelled)
        reports = [read_json(directory / "performance.json") for directory in directories]
        phases: dict[str, float] = {}
        for report in reports:
            values = report.get("phaseTimingsSeconds")
            if not isinstance(values, dict):
                raise ValueError(f"Scanner report requires phase timings: {report}")
            for phase, value in values.items():
                if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"Invalid scanner timing: phase={phase}, value={value}")
                phases[phase] = phases.get(phase, 0.0) + value
        write_index(staging[2], {"format": "EndfieldHelperPerformanceReport/1", "helper": "EndfieldBundleScannerShards",
                               "status": "complete", "elapsedSeconds": time.perf_counter() - started,
                               "phaseTimingsSeconds": phases, "capacity": asdict(capacity), "range": {"start": start, "end": end},
                               "phaseCounters": {"scanned": end - start, "workers": len(shards)}, "events": events,
                               "phaseSemantics": "sum of per-child call spans; parallel spans overlap; I/O is logical process transfer bytes",
                               "peakSemantics": "sum of per-child peaks, not simultaneous resident peak",
                               "memoryBudgetSemantics": "startup concurrency reservation; does not terminate a large bundle that exceeds an estimate",
                               "overReservationShards": [number for number, value in metrics.items()
                                                         if value.get("peakWorkingSetBytes", 0) > capacity.worker_budget_bytes],
                               "shards": [{**asdict(shard), "performance": str(directory / "performance.json"),
                                           "console": str(directory / "console.log")} for shard, directory in zip(shards, directories, strict=True)],
                               **{key: sum(value.get(key, 0) for value in metrics.values()) for key in ("peakWorkingSetBytes", "readBytes", "writeBytes")}})
        check_cancelled()
        with log.open("x", encoding="utf-8") as output:
            for directory in directories:
                output.write(f"Shard console: {directory / 'console.log'}\n")
        for temporary, target in zip(staging, targets, strict=True):
            temporary.replace(target)
    except BaseException:
        for path in (*staging, *targets):
            path.unlink(missing_ok=True)
        raise
