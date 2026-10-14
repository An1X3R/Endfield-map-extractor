"""Overlap ordered preparation with bounded, cancellable external scene workers."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Event
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


class ScenePipelineCancelled(RuntimeError):
    """Stop a sibling worker after the coordinator has already failed."""


class SceneWorkerCleanupError(RuntimeError):
    """An external worker could not confirm that its child process exited."""


def run_scene_pipeline(count: int, capacity: int, prepare: Callable[[int, Callable[[], None]], T],
                       execute: Callable[[T, Callable[[], None]], R], can_overlap: Callable[[T, tuple[T, ...]], bool],
                       check_cancelled: Callable[[], None], completed: Callable[[int, R], None]) -> list[R]:
    """Keep preparation on its owner thread and preserve output ordering across workers."""
    if count < 0 or capacity not in (1, 2):
        raise ValueError("Scene pipeline requires a nonnegative batch count and one or two workers")
    stopped = Event()
    pending: dict[Future[R], tuple[int, T]] = {}
    results: dict[int, R] = {}

    def worker_check() -> None:
        if stopped.is_set():
            raise ScenePipelineCancelled("Scene pipeline stopped after cancellation or another batch failure")
        check_cancelled()

    def coordinator_check() -> None:
        check_cancelled()
        for future in pending:
            if future.done():
                error = future.exception()
                if error is not None:
                    raise error

    def collect(timeout: float) -> None:
        coordinator_check()
        done, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
        for future in sorted(done, key=lambda item: pending[item][0]):
            index, _ = pending.pop(future)
            result = future.result()
            results[index] = result
            completed(index, result)

    executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="endfield-scene")
    failure: BaseException | None = None
    try:
        for index in range(count):
            collect(0.0)
            item = prepare(index, coordinator_check)
            coordinator_check()
            while pending and (len(pending) >= capacity or not can_overlap(item, tuple(row[1] for row in pending.values()))):
                collect(0.2)
            coordinator_check()
            pending[executor.submit(execute, item, worker_check)] = (index, item)
        while pending:
            collect(0.2)
    except BaseException as error:
        failure = error
        raise
    finally:
        stopped.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        cleanup_errors = [error for future in pending if not future.cancelled()
                          and isinstance(error := future.exception(), SceneWorkerCleanupError)
                          and error is not failure]
        if cleanup_errors:
            failures = ([failure] if failure is not None else []) + cleanup_errors
            detail = "; ".join(str(error) for error in cleanup_errors)
            raise BaseExceptionGroup(f"Scene worker cleanup failed: {detail}", failures)
    return [results[index] for index in range(count)]
