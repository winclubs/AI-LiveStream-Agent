import asyncio
import threading

import pytest

from server.core.cpu_worker import (
    CPU_WORKER_CAPACITY,
    CPU_WORKER_THREADS,
    cpu_worker_snapshot,
    run_cpu_bound,
)


def test_cpu_work_runs_off_event_loop_and_respects_shared_bounds():
    async def exercise():
        loop_thread = threading.get_ident()
        release = threading.Event()
        started = threading.Event()
        active = 0
        peak = 0
        state_lock = threading.Lock()

        def blocking_work():
            nonlocal active, peak
            worker_thread = threading.get_ident()
            with state_lock:
                active += 1
                peak = max(peak, active)
                if peak == CPU_WORKER_THREADS:
                    started.set()
            release.wait(timeout=5)
            with state_lock:
                active -= 1
            return worker_thread

        tasks = [asyncio.create_task(run_cpu_bound(blocking_work)) for _ in range(CPU_WORKER_CAPACITY + 3)]
        try:
            assert await asyncio.to_thread(started.wait, 2)
            snapshot = cpu_worker_snapshot()
            assert snapshot["admitted"] <= CPU_WORKER_CAPACITY
            assert snapshot["active"] <= CPU_WORKER_THREADS
        finally:
            release.set()
        worker_threads = await asyncio.gather(*tasks)
        return loop_thread, worker_threads, peak

    loop_thread, worker_threads, peak = asyncio.run(exercise())
    assert all(thread_id != loop_thread for thread_id in worker_threads)
    assert peak == CPU_WORKER_THREADS


def test_cancellation_waits_for_cpu_work_before_releasing_file_owner(tmp_path):
    async def exercise():
        owned_file = tmp_path / "owned.part"
        owned_file.write_text("payload", encoding="utf-8")
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def work():
            started.set()
            release.wait(timeout=5)
            assert owned_file.exists()
            finished.set()

        task = asyncio.create_task(run_cpu_bound(work))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        owned_file.unlink()
        assert not owned_file.exists()

    asyncio.run(exercise())


def test_rag_async_search_runs_in_worker_and_serializes_state_access():
    from server.core.rag.engine import KnowledgeBaseEngine

    async def exercise():
        engine = KnowledgeBaseEngine()
        loop_thread = threading.get_ident()
        lock = threading.Lock()
        active = 0
        peak = 0
        worker_threads = []

        def tracked_search(query, top_k=3, min_score=None):
            import time
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                worker_threads.append(threading.get_ident())
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"query": query, "hits": [], "best_score": 0.0, "matched": False}

        engine.search = tracked_search
        await asyncio.gather(engine.search_async("one"), engine.search_async("two"))
        return loop_thread, worker_threads, peak

    loop_thread, worker_threads, peak = asyncio.run(exercise())
    assert worker_threads and all(thread_id != loop_thread for thread_id in worker_threads)
    assert peak == 1


def test_admission_bound_is_shared_across_event_loops():
    import time

    release = threading.Event()
    errors = []

    def work():
        release.wait(timeout=5)

    def run_loop():
        async def submit_many():
            await asyncio.gather(*(run_cpu_bound(work) for _ in range(CPU_WORKER_CAPACITY)))

        try:
            asyncio.run(submit_many())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_loop) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 2
        while cpu_worker_snapshot()["admitted"] < CPU_WORKER_CAPACITY and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cpu_worker_snapshot()["admitted"] == CPU_WORKER_CAPACITY
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
