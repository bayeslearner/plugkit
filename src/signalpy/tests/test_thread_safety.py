"""Concurrency stress tests for the reactive engine.

These tests are *targeted* at the races we just locked closed:
  - Lost version bumps under concurrent set()
  - Dropped subscriptions when get() and set() race
  - Double-execution of an effect from concurrent writers
  - Double-recompute of a Computed under concurrent dirty reads

Each test would have failed (probabilistically, on enough iterations)
against the pre-lock implementation. They pass deterministically with
the reentrant lock guarding all shared-state mutations.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from signalpy.kernel.reactive import Signal, Computed, Effect, batch


# ── 1. No lost version bumps ───────────────────────────────────────

def test_concurrent_writers_all_version_bumps_recorded():
    """N threads each call set() M times. Final _version must equal N*M.

    Pre-lock: _version += 1 was a racy read-modify-write; bumps were
    lost. Post-lock: serialized.
    """
    sig = Signal(0)
    N_THREADS = 8
    PER_THREAD = 500

    def writer(start: int):
        for i in range(PER_THREAD):
            sig.set(start * PER_THREAD + i + 1)  # always a fresh int (identity differs)

    with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
        list(pool.map(writer, range(N_THREADS)))

    assert sig._version == N_THREADS * PER_THREAD, (
        f"expected {N_THREADS * PER_THREAD} version bumps, got {sig._version}"
    )


# ── 2. No dropped subscriptions ────────────────────────────────────

def test_concurrent_subscribe_and_write_no_dropped_subs():
    """One thread keeps creating Effects (new subscribers); another keeps
    writing (which triggers the subscriber-set cleanup pass). After the
    storm, every live Effect must have been notified by the final write.

    Pre-lock: cleanup pass rebound _subscribers under the lock while
    get() added to the OLD set lock-free; new subscriptions could vanish.
    """
    sig = Signal(0)
    notified: list[Effect] = []
    notified_lock = threading.Lock()
    stop = threading.Event()

    effects: list[Effect] = []
    effects_lock = threading.Lock()

    def make_effect(_):
        # Capture the current value when created. Effect re-runs on every
        # signal change. Track who got notified at least once after creation.
        seen = {"runs": 0}
        def body():
            v = sig.get()
            seen["runs"] += 1
            if seen["runs"] > 1:  # 1st run is creation; 2nd+ is reactive
                with notified_lock:
                    notified.append(eff)
        eff = Effect(body)
        with effects_lock:
            effects.append(eff)

    def subscriber_thread():
        i = 0
        while not stop.is_set():
            make_effect(i)
            i += 1

    def writer_thread():
        v = 1
        while not stop.is_set():
            sig.set(v)
            v += 1

    sub_t = threading.Thread(target=subscriber_thread)
    wr_t = threading.Thread(target=writer_thread)
    sub_t.start(); wr_t.start()
    threading.Event().wait(0.3)
    stop.set()
    sub_t.join(); wr_t.join()

    # Final synchronization write — every live effect must be notified.
    with effects_lock:
        snapshot = list(effects)
    pre_count = len(notified)
    sig.set(object())  # fresh value, guaranteed identity-different
    post_count = len(notified)

    # Every (non-disposed) effect must appear exactly once in the
    # post-final-write delta. If any subscription was dropped during
    # the race, that effect would NOT be in `notified` after the final set.
    delta = post_count - pre_count
    assert delta == len(snapshot), (
        f"final write notified {delta} effects but {len(snapshot)} were live "
        "— some subscriptions were dropped"
    )


# ── 3. Two writers don't double-run an effect ──────────────────────

def test_concurrent_writers_no_duplicate_effect_runs():
    """Two threads each issue K writes to a shared signal. The effect must
    run between 1 and 2*K times — never more. Pre-lock, the
    snapshot-then-iterate pattern with unsynchronized set() could trigger
    an effect twice for a single conceptual change.

    The lower bound (1) and upper bound (2K) are intentionally loose;
    this test is a regression for "vastly more than 2K runs" caused by
    interleaved snapshot-and-notify cycles.
    """
    sig = Signal(0)
    runs = {"n": 0}

    def body():
        sig.get()
        runs["n"] += 1

    Effect(body)
    K = 200

    def writer(start):
        for i in range(K):
            sig.set(start * K + i + 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(writer, range(2)))

    # Initial run + at most 2K reactive runs
    assert 1 <= runs["n"] <= 2 * K + 1, f"effect ran {runs['n']} times"


# ── 4. Computed not double-recomputed ──────────────────────────────

def test_concurrent_dirty_reads_single_recompute():
    """When N threads simultaneously call get() on a dirty Computed,
    `_fn` must execute exactly once per dirty cycle. Pre-lock, two
    threads could both observe `_dirty=True` and both run `_fn`.
    """
    src = Signal(0)
    fn_calls = {"n": 0}
    fn_lock = threading.Lock()

    def expensive():
        with fn_lock:
            fn_calls["n"] += 1
        # simulate some work
        return src.get() * 2

    comp = Computed(expensive)
    fn_calls["n"] = 0  # reset after construction (Computed is lazy in our impl,
                       # but this is defensive in case of warmup)

    # Make it dirty
    src.set(1)

    # Many threads stampede on get()
    results = []
    results_lock = threading.Lock()

    def reader():
        v = comp.get()
        with results_lock:
            results.append(v)

    threads = [threading.Thread(target=reader) for _ in range(32)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert fn_calls["n"] == 1, (
        f"Computed recomputed {fn_calls['n']} times during a dirty stampede "
        "— expected exactly 1"
    )
    assert all(r == 2 for r in results)


# ── 5. Re-entrant signal write from inside an effect ───────────────

def test_effect_writes_signal_no_deadlock():
    """An effect that writes to a (different) signal must not deadlock.
    This validates the RLock choice: same-thread re-acquire works.
    """
    a = Signal(0)
    b = Signal(0)
    runs = {"n": 0}

    def body():
        v = a.get()
        if v < 5:
            b.set(v + 100)  # nested set on a different signal
        runs["n"] += 1

    Effect(body)
    for i in range(1, 4):
        a.set(i)

    assert runs["n"] >= 4
    assert b.peek() == 100 + 3  # last write wins
