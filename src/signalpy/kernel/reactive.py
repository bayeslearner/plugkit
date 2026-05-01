"""Reactive primitives — Signal, Computed, Effect, batch.

The reactivity engine. Zero external dependencies. ~350 lines.
Everything in the kernel builds on these three primitives.

Algorithm (Vue 3 / Preact Signals model):
  - A context variable `_active_consumer` tracks who is currently executing.
  - Signal.get() checks _active_consumer and registers the dependency.
  - Signal.set() notifies all registered consumers.
  - Computed is a lazy Signal that recomputes when dependencies are dirty.
    Skips propagation if the recomputed value is identical (by identity).
  - Effect re-runs its function when any tracked dependency changes.
  - batch() groups multiple set() calls, effects fire once at the end.

Threading: a single global RLock (`_lock`) serializes all mutations of
shared reactive state — Signal value/version updates, subscriber-set
adds and removals, dependency tracking, batch depth/queue, and the
Computed recompute critical section. The lock is reentrant so an effect
that writes to a signal it reads (or any same-thread re-entry) does not
deadlock. Effect/Computed bodies execute *with the lock held* on the
running thread, which means signal mutation across threads is fully
serialized. This trades raw throughput for simple, audit-able correctness.

Safe for: multi-threaded use (ThreadPoolExecutor for blocking I/O,
threaded REST servers, background poller threads writing to shared
signals) and CPython 3.13+ free-threaded mode (no reliance on GIL
atomicity for set/dict mutations — every mutation is under the lock).

Ordering: Effects execute in creation order within a batch flush.
This ensures parent-before-child and deterministic behavior.

Async effects: tracking only covers signal reads before the first
`await`. Reads after an `await` may run in a different microtask where
_active_consumer has been reset. This is a known limitation shared
with Vue 3 (which doesn't support async watchEffect).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# ── Global reactive state ─────────────────────────────────────────

_active_consumer: ContextVar[_Consumer | None] = ContextVar("_active_consumer", default=None)
_batch_depth: int = 0
_batch_queue: list[Effect] = []
_lock = threading.RLock()
_creation_counter: int = 0


def _next_id() -> int:
    """Monotonic creation counter for deterministic effect ordering."""
    global _creation_counter
    with _lock:
        _creation_counter += 1
        return _creation_counter


class _Consumer:
    """Base for anything that tracks Signal dependencies (Effect, Computed)."""
    __slots__ = ("_deps", "_id")

    def __init__(self) -> None:
        self._deps: set[Signal] = set()
        self._id: int = _next_id()

    def _track(self, signal: Signal) -> None:
        """Record that this consumer read the given signal.

        Caller holds `_lock` (called from Signal.get / Computed.get under lock).
        """
        self._deps.add(signal)

    def _untrack_all(self) -> None:
        """Remove self from all tracked signals' subscriber lists.

        Caller holds `_lock` (called from Effect.run / Computed._recompute /
        dispose paths, all of which acquire the lock).
        """
        for sig in self._deps:
            sig._subscribers.discard(self)
        self._deps.clear()

    def _notify(self) -> None:
        """Called by a signal when its value changes. Override in subclass."""
        raise NotImplementedError


# ── Signal ─────────────────────────────────────────────────────────

class Signal(Generic[T]):
    """Reactive value container.

    Reading tracks the current consumer. Writing notifies all dependents.

        counter = Signal(0)
        counter.set(5)       # notifies all consumers
        v = counter.get()    # tracks caller if inside Effect/Computed
        v = counter.peek()   # read without tracking
    """
    __slots__ = ("_value", "_version", "_subscribers")

    def __init__(self, value: T) -> None:
        self._value: T = value
        self._version: int = 0
        self._subscribers: set[_Consumer] = set()

    def get(self) -> T:
        """Read the value. If inside an Effect or Computed, track the dependency.

        Duplicate tracking is a no-op: _deps and _subscribers are sets.
        Thread-safe: tracking writes and the value read happen under the lock,
        so a concurrent set() cannot drop the new subscription nor expose a
        torn read.
        """
        consumer = _active_consumer.get()
        with _lock:
            if consumer is not None:
                consumer._track(self)
                self._subscribers.add(consumer)
            return self._value

    def peek(self) -> T:
        """Read the value without tracking. For use outside reactive contexts.

        Lock-free fast path: a single attribute read. Safe under the GIL
        (atomic reference load) and under PEP 703 free-threading (loads of
        object references are atomic per the no-GIL spec).
        """
        return self._value

    def set(self, value: T) -> None:
        """Set the value. If changed (by identity), notify all subscribers.

        Identity comparison (is) is deliberate:
        - Mutating a dict in place and calling set() with the same object is a no-op.
        - Create a new dict to trigger notification.
        - Consistent with Vue 3's Object.is() and Preact's identity check.

        Thread-safe: the entire compare/swap/version-bump/notify sequence runs
        under the reentrant lock so two writers serialize cleanly. Each writer
        gets exactly one version bump and exactly one notification cycle; no
        lost updates, no duplicate effect runs caused by interleaved writes.
        """
        with _lock:
            if value is self._value:
                return
            self._value = value
            self._version += 1
            self._notify_subscribers()

    def _notify_subscribers(self) -> None:
        """Notify all consumers that this signal changed.

        Wrapped in an implicit batch so cascading notifications coalesce:
        a single ``Signal.set`` produces one effect run per dependent
        effect, even through diamond dependency graphs (A → B → D and
        A → C → D would otherwise notify D twice). Effects also flush
        in creation order rather than ``set`` iteration order.

        Thread-safe: takes a snapshot of subscribers under lock.
        Cleans up disposed consumers to prevent memory leaks.

        All consumers get _notify() called — batch checking is handled
        by each consumer type:
        - Effect._notify() checks _batch_depth and enqueues if batching
        - Computed._notify() marks dirty and propagates to its subscribers
        This ensures Computed→Effect chains work correctly in batch mode.
        """
        with _lock:
            # Clean up disposed consumers (prevents memory leak)
            self._subscribers = {
                c for c in self._subscribers
                if not getattr(c, '_disposed', False)
            }
            snapshot = list(self._subscribers)

        with batch():
            for consumer in snapshot:
                consumer._notify()

    @property
    def value(self) -> T:
        """Property alias for get()."""
        return self.get()

    @value.setter
    def value(self, v: T) -> None:
        """Property alias for set()."""
        self.set(v)

    def __repr__(self) -> str:
        return f"Signal({self._value!r})"


# ── Computed ───────────────────────────────────────────────────────

class Computed(_Consumer, Generic[T]):
    """Derived reactive value. Caches result. Recomputes lazily when deps change.

    Skips propagation to downstream subscribers if the recomputed value
    is identical (by identity) to the previous value. This avoids
    unnecessary effect re-runs when a computed returns the same object.

        name = Signal("Alice")
        greeting = Computed(lambda: f"Hello, {name.get()}!")
        greeting.get()  # "Hello, Alice!" — cached
        name.set("Bob")
        greeting.get()  # "Hello, Bob!" — recomputed
    """
    __slots__ = ("_fn", "_value", "_dirty", "_version", "_subscribers", "_disposed")

    def __init__(self, fn: Callable[[], T]) -> None:
        super().__init__()
        self._fn = fn
        self._value: T | None = None
        self._dirty: bool = True
        self._version: int = 0
        self._subscribers: set[_Consumer] = set()
        self._disposed: bool = False

    def get(self) -> T:
        """Read the computed value. Recomputes if dirty. Tracks caller.

        Thread-safe: dirty-check, recompute, and subscriber registration run
        under the reentrant lock so two threads cannot both observe `_dirty`
        and double-execute `_fn`.
        """
        with _lock:
            if self._disposed:
                return self._value  # type: ignore
            if self._dirty:
                self._recompute()
            # Track this computed as a dependency of the outer consumer
            consumer = _active_consumer.get()
            if consumer is not None:
                consumer._track(self)  # type: ignore  # Computed acts as a Signal
                self._subscribers.add(consumer)
            return self._value  # type: ignore

    def peek(self) -> T:
        """Read without tracking."""
        with _lock:
            if self._dirty and not self._disposed:
                self._recompute()
            return self._value  # type: ignore

    def _recompute(self) -> None:
        """Execute fn, track new dependencies, cache result.

        Only bumps version and propagates to subscribers if the
        recomputed value is different (by identity) from the old value.
        This is the key optimization: if a computed returns the same
        object, downstream effects don't re-run.

        The body executes inside an implicit ``batch()`` so any signal
        writes the body performs (rare but legal) coalesce instead of
        firing one notification cascade per write.

        Caller must hold `_lock` (this is internal; entered from get/peek
        which acquire it, or from _notify which we also lock).
        """
        self._untrack_all()
        token = _active_consumer.set(self)
        try:
            with batch():
                old = self._value
                self._value = self._fn()
                self._dirty = False
                # Only propagate if value actually changed (identity)
                if self._value is not old:
                    self._version += 1
                    self._propagate()
        finally:
            _active_consumer.reset(token)

    def _propagate(self) -> None:
        """Notify downstream subscribers that this computed's value changed.

        Wrapped in an implicit batch (same reasoning as
        ``Signal._notify_subscribers``): if a Computed value changes
        outside an explicit batch, dependent effects still coalesce.
        """
        with _lock:
            # Clean up disposed subscribers
            self._subscribers = {
                c for c in self._subscribers
                if not getattr(c, '_disposed', False)
            }
            snapshot = list(self._subscribers)
        with batch():
            for consumer in snapshot:
                consumer._notify()

    def _notify(self) -> None:
        """Called when a dependency changed. Mark dirty and propagate.

        We must propagate even though we haven't recomputed yet, because
        downstream Effects need to know they should re-run. When they do
        re-run and read this Computed, _recompute will check if the value
        actually changed and skip further propagation if not.

        Thread-safe: dirty-toggle and subscriber snapshot are under the
        reentrant lock; the dedup ("only propagate if not already dirty")
        is now race-free.
        """
        with _lock:
            if self._disposed:
                return
            was_dirty = self._dirty
            self._dirty = True
            if was_dirty:
                return
            snapshot = list(self._subscribers)
        for consumer in snapshot:
            consumer._notify()

    def dispose(self) -> None:
        """Stop tracking. Remove from all dependency subscriber lists."""
        with _lock:
            self._disposed = True
            self._untrack_all()
            self._subscribers.clear()

    @property
    def value(self) -> T:
        return self.get()

    def __repr__(self) -> str:
        return f"Computed({self._value!r}, dirty={self._dirty})"


# ── Effect ─────────────────────────────────────────────────────────

class Effect(_Consumer):
    """Reactive side effect. Re-runs when tracked dependencies change.

        counter = Signal(0)
        log_effect = Effect(lambda: print(f"Counter: {counter.get()}"))
        counter.set(1)  # prints "Counter: 1"
        log_effect.dispose()  # stops tracking

    Async effects (async def) are supported but with a limitation:
    dependency tracking only covers signal reads before the first `await`.
    Reads after `await` may execute in a different microtask where
    _active_consumer has been reset. Design effects to read all signals
    upfront, then await with the captured values.

    Async supersede semantics: if a notification arrives while an async
    body is mid-await, the engine sets ``_pending_run`` instead of
    dropping the notification. When the body finishes, the effect runs
    again automatically — so the body always converges on the latest
    state. Inside the body, ``is_stale()`` returns True once a newer
    run has been scheduled, so the body can short-circuit voluntarily.
    With ``cancel_on_supersede=True``, the engine calls
    ``task.cancel()`` on the in-flight task at supersede time — the
    body sees ``CancelledError`` at the next await point.
    """
    __slots__ = (
        "_fn", "_is_async", "_disposed", "_running",
        "_pending_run", "_in_flight_task", "_cancel_on_supersede",
    )

    def __init__(
        self,
        fn: Callable,
        *,
        lazy: bool = False,
        cancel_on_supersede: bool = False,
    ) -> None:
        super().__init__()
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)
        self._disposed = False
        self._running = False
        self._pending_run = False
        self._in_flight_task: asyncio.Task | None = None
        self._cancel_on_supersede = cancel_on_supersede
        if not lazy:
            self.run()

    def run(self) -> None:
        """Execute the effect function, tracking all Signal reads.

        The body runs inside an implicit ``batch()`` so any signal writes
        the body performs coalesce: e.g. an effect that updates two
        signals in sequence triggers a single downstream effect flush
        instead of two. Async bodies enter their own batch inside the
        spawned task (batch state is per-flow because depth is global —
        see threading model docs for the caveat).

        Re-entrancy guard: if the effect is already running (e.g., effect
        writes to a signal it reads), skip. This prevents infinite loops
        AND prevents two threads from both entering the same effect body.

        For async effects: we set _active_consumer BEFORE creating the task.
        asyncio.create_task copies the current context, so the coroutine
        body will see _active_consumer=self during its Signal reads.
        Reads after the first `await` may not be tracked (Vue 3 limitation).

        Thread-safety: the entire run() body is under the reentrant lock.
        That serializes effect execution across threads, which is what makes
        re-entrant signal writes from inside the effect safe (same thread
        can re-acquire) and prevents two writers from running an effect twice
        with interleaved reads.
        """
        with _lock:
            if self._disposed:
                return
            if self._running:
                # Already running. For ASYNC effects, mark a pending re-run
                # so the body re-fires once the in-flight task finishes —
                # this is the fix for "notification arrived mid-await and
                # got silently dropped." Optionally cancel the in-flight
                # task at the user's request.
                #
                # For SYNC effects, the only way to land here is re-entry
                # from inside our own body's signal write under the global
                # lock — keep the old silent-drop behavior, which matches
                # what users have relied on (the body in-flight already
                # observed the new value on its current read pass).
                if self._is_async:
                    self._pending_run = True
                    if self._cancel_on_supersede and self._in_flight_task is not None:
                        if not self._in_flight_task.done():
                            self._in_flight_task.cancel()
                return
            self._running = True
            self._pending_run = False
            self._untrack_all()

            if self._is_async:
                token = _active_consumer.set(self)
                self_ref = self

                async def _tracked_async():
                    try:
                        # Note: no implicit ``with batch():`` here. ``_batch_depth``
                        # is process-global, so a long-running async body would
                        # hold depth>0 across its awaits and swallow other tasks'
                        # cascades — including the very supersede notification
                        # that should set ``_pending_run`` mid-await. The cost
                        # of skipping the batch is that an async body that
                        # writes two signals back-to-back may fire dependent
                        # effects twice; if you need coalescing, wrap the
                        # writes explicitly in ``with batch():``.
                        await self_ref._fn()
                    except asyncio.CancelledError:
                        # Superseded via cancel_on_supersede. The replacement
                        # run is already queued via _pending_run; let the
                        # finally block schedule it.
                        pass
                    except Exception:
                        log.exception("Async effect execution error")
                    finally:
                        with _lock:
                            self_ref._running = False
                            self_ref._in_flight_task = None
                            should_rerun = self_ref._pending_run and not self_ref._disposed
                            self_ref._pending_run = False
                        if should_rerun:
                            self_ref.run()

                try:
                    loop = asyncio.get_running_loop()
                    self._in_flight_task = loop.create_task(_tracked_async())
                except RuntimeError:
                    self._running = False
                finally:
                    _active_consumer.reset(token)
            else:
                token = _active_consumer.set(self)
                try:
                    with batch():
                        self._fn()
                except Exception:
                    log.exception("Effect execution error")
                finally:
                    _active_consumer.reset(token)
                    self._running = False
                    # Sync effects never set _pending_run (the running-guard
                    # only sets it for async), so no drain is needed here.

    def _notify(self) -> None:
        """Called by a signal when its value changes.

        Thread-safe: batch-state inspection and queue insertion are atomic
        under the reentrant lock; non-batch path calls run() which takes
        the lock internally.
        """
        if self._disposed:
            return
        with _lock:
            if _batch_depth > 0:
                if self not in _batch_queue:
                    _batch_queue.append(self)
                return
        self.run()

    def dispose(self) -> None:
        """Stop tracking. Remove from all dependency subscriber lists."""
        with _lock:
            self._disposed = True
            self._untrack_all()

    def __repr__(self) -> str:
        return f"Effect({self._fn.__name__ if hasattr(self._fn, '__name__') else '...'})"


def is_stale() -> bool:
    """Has a newer run been scheduled while *the currently running effect's*
    body is in flight?

    Useful inside an async effect body to short-circuit voluntarily:

        @effect
        async def index(self):
            items = self.rt.items.get()
            for item in items:
                await process(item)
                if is_stale():
                    return  # newer items arrived; drop the rest

    Returns ``False`` outside any effect, and ``False`` for sync effects
    (sync bodies cannot be superseded mid-execution under the global
    lock — the running guard catches re-entry differently).
    """
    consumer = _active_consumer.get()
    if isinstance(consumer, Effect):
        return consumer._pending_run
    return False


# ── Batch ──────────────────────────────────────────────────────────

@contextmanager
def batch():
    """Group signal changes. Effects fire once when the batch ends.

        with batch():
            name.set("Alice")
            age.set(30)
        # Effects that depend on name or age run ONCE here, not twice.

    Thread-safe: batch depth is protected by lock.
    """
    global _batch_depth
    with _lock:
        _batch_depth += 1
    try:
        yield
    finally:
        with _lock:
            _batch_depth -= 1
            should_flush = _batch_depth == 0
        if should_flush:
            _flush_batch()


def _flush_batch() -> None:
    """Run all pending effects from the batch queue.

    Effects are sorted by creation order (_id) for deterministic execution.
    Parent effects (created first) run before child effects.
    """
    global _batch_queue
    iterations = 0
    while True:
        with _lock:
            if not _batch_queue:
                break
            # Sort by creation order (deterministic, parent before child)
            _batch_queue.sort(key=lambda e: e._id)
            pending = list(_batch_queue)
            _batch_queue.clear()

        if iterations > 100:
            log.error("Reactive batch exceeded 100 iterations — possible infinite loop")
            with _lock:
                _batch_queue.clear()
            break

        for effect in pending:
            if not effect._disposed:
                effect.run()
        iterations += 1


