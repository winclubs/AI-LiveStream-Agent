"""Shared bounded executor for CPU-heavy request work.

Submitted work has a deliberately small process-wide pool and bounded admission.
Cancellation is delivered only after an admitted callable finishes, so callers may
safely release temporary files in their ``finally`` blocks.
"""
from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")

CPU_WORKER_THREADS = 2
CPU_WORKER_CAPACITY = 4

_executor = ThreadPoolExecutor(max_workers=CPU_WORKER_THREADS, thread_name_prefix="live-agent-cpu")
_admission = threading.BoundedSemaphore(CPU_WORKER_CAPACITY)
_metrics_lock = threading.Lock()
_admitted = 0
_active = 0
_peak_admitted = 0
_peak_active = 0
_submitted = 0
_completed = 0


def _execute(call: Callable[[], T]) -> T:
    global _active, _peak_active, _completed
    with _metrics_lock:
        _active += 1
        _peak_active = max(_peak_active, _active)
    try:
        return call()
    finally:
        with _metrics_lock:
            _active -= 1
            _completed += 1


async def run_cpu_bound(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run one callable outside the event loop with bounded process-wide admission.

    Once submitted, cancellation waits for the callable to finish before propagating.
    This prevents route cleanup from deleting files still in use by a worker.
    """
    global _admitted, _peak_admitted, _submitted
    while not _admission.acquire(blocking=False):
        await asyncio.sleep(0.005)
    with _metrics_lock:
        _admitted += 1
        _submitted += 1
        _peak_admitted = max(_peak_admitted, _admitted)
    try:
        future = asyncio.get_running_loop().run_in_executor(
            _executor,
            functools.partial(_execute, functools.partial(func, *args, **kwargs)),
        )
    except BaseException:
        with _metrics_lock:
            _admitted -= 1
        _admission.release()
        raise
    cancelled: asyncio.CancelledError | None = None
    try:
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
            except BaseException:
                if cancelled is not None:
                    raise cancelled
                raise
        if cancelled is not None:
            raise cancelled
        return result
    finally:
        with _metrics_lock:
            _admitted -= 1
        _admission.release()


def cpu_worker_snapshot() -> dict[str, int]:
    """Return thread-safe lifetime metrics for health tests and diagnostics."""
    with _metrics_lock:
        return {
            "threads": CPU_WORKER_THREADS,
            "capacity": CPU_WORKER_CAPACITY,
            "admitted": _admitted,
            "active": _active,
            "peak_admitted": _peak_admitted,
            "peak_active": _peak_active,
            "submitted": _submitted,
            "completed": _completed,
        }
