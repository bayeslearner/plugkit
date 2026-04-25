"""Reactive primitives — Signal, Computed, Effect, batch.

The reactivity engine. Zero dependencies. ~200 lines.
Everything in the kernel builds on these three primitives.

Algorithm (Vue 3 / Preact Signals model):
  - A context variable `_active_consumer` tracks who is currently executing.
  - Signal.get() checks _active_consumer and registers the dependency.
  - Signal.set() notifies all registered consumers.
  - Computed is a lazy Signal that recomputes when dependencies are dirty.
  - Effect re-runs its function when any tracked dependency changes.
  - batch() groups multiple set() calls, effects fire once at the end.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# ── Global reactive state ─────────────────────────────────────────

_active_consumer: ContextVar[_Consumer | None] = ContextVar("_active_consumer", default=None)
_batch_depth: int = 0
_batch_queue: list[Effect] = []


class _Consumer:
    """Base for anything that tracks Signal dependencies (Effect, Computed)."""
    __slots__ = ("_deps",)

    def __init__(self) -> None:
        self._deps: set[Signal] = set()

    def _track(self, signal: Signal) -> None:
        """Record that this consumer read the given signal."""
        self._deps.add(signal)

    def _untrack_all(self) -> None:
        """Remove self from all tracked signals' subscriber lists."""
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
        """Read the value. If inside an Effect or Computed, track the dependency."""
        consumer = _active_consumer.get()
        if consumer is not None:
            consumer._track(self)
            self._subscribers.add(consumer)
        return self._value

    def peek(self) -> T:
        """Read the value without tracking. For use outside reactive contexts."""
        return self._value

    def set(self, value: T) -> None:
        """Set the value. If changed, notify all subscribers."""
        if value is self._value:
            return
        self._value = value
        self._version += 1
        self._notify_subscribers()

    def update(self, fn: Callable[[T], T]) -> None:
        """Update value based on current: signal.update(lambda x: x + 1)"""
        self.set(fn(self._value))

    def _notify_subscribers(self) -> None:
        """Notify all consumers that this signal changed."""
        global _batch_depth
        for consumer in list(self._subscribers):
            if _batch_depth > 0:
                if isinstance(consumer, Effect) and consumer not in _batch_queue:
                    _batch_queue.append(consumer)
                elif isinstance(consumer, Computed):
                    consumer._dirty = True
            else:
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
        """Read the computed value. Recomputes if dirty. Tracks caller."""
        if self._disposed:
            return self._value  # type: ignore
        if self._dirty:
            self._recompute()
        # Track this computed as a dependency of the outer consumer
        consumer = _active_consumer.get()
        if consumer is not None:
            consumer._track(self)  # type: ignore  # Computed acts as a Signal for tracking
            self._subscribers.add(consumer)
        return self._value  # type: ignore

    def peek(self) -> T:
        """Read without tracking."""
        if self._dirty and not self._disposed:
            self._recompute()
        return self._value  # type: ignore

    def _recompute(self) -> None:
        """Execute fn, track new dependencies, cache result."""
        # Untrack old dependencies
        self._untrack_all()
        # Set self as the active consumer
        token = _active_consumer.set(self)
        try:
            old = self._value
            self._value = self._fn()
            if self._value is not old:
                self._version += 1
            self._dirty = False
        finally:
            _active_consumer.reset(token)

    def _notify(self) -> None:
        """Called when a dependency changed. Mark dirty and propagate."""
        if self._disposed:
            return
        self._dirty = True
        # Propagate to our own subscribers (other Computeds or Effects)
        for consumer in list(self._subscribers):
            consumer._notify()

    def dispose(self) -> None:
        """Stop tracking. Remove from all dependency subscriber lists."""
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
    """
    __slots__ = ("_fn", "_is_async", "_disposed", "_running")

    def __init__(self, fn: Callable, *, lazy: bool = False) -> None:
        super().__init__()
        self._fn = fn
        self._is_async = inspect.iscoroutinefunction(fn)
        self._disposed = False
        self._running = False
        if not lazy:
            self.run()

    def run(self) -> None:
        """Execute the effect function, tracking all Signal reads.

        For async effects: we set _active_consumer BEFORE creating the task.
        asyncio.create_task copies the current context, so the coroutine
        body will see _active_consumer=self during its Signal reads.
        We reset the context var immediately after task creation (the task
        has its own copy).
        """
        if self._disposed or self._running:
            return
        self._running = True
        self._untrack_all()

        if self._is_async:
            # Set context BEFORE create_task so the task inherits it
            token = _active_consumer.set(self)
            self_ref = self

            async def _tracked_async():
                try:
                    await self_ref._fn()
                except Exception:
                    log.exception("Async effect execution error")
                finally:
                    self_ref._running = False

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_tracked_async())
            except RuntimeError:
                self._running = False
            finally:
                # Reset in the CALLING context (not the task's context)
                _active_consumer.reset(token)
        else:
            token = _active_consumer.set(self)
            try:
                self._fn()
            except Exception:
                log.exception("Effect execution error")
            finally:
                _active_consumer.reset(token)
                self._running = False

    def _notify(self) -> None:
        """Called by a signal when its value changes."""
        if self._disposed:
            return
        global _batch_depth
        if _batch_depth > 0:
            if self not in _batch_queue:
                _batch_queue.append(self)
        else:
            self.run()

    def dispose(self) -> None:
        """Stop tracking. Remove from all dependency subscriber lists."""
        self._disposed = True
        self._untrack_all()

    def __repr__(self) -> str:
        return f"Effect({self._fn.__name__ if hasattr(self._fn, '__name__') else '...'})"


# ── Batch ──────────────────────────────────────────────────────────

@contextmanager
def batch():
    """Group signal changes. Effects fire once when the batch ends.

        with batch():
            name.set("Alice")
            age.set(30)
        # Effects that depend on name or age run ONCE here, not twice.
    """
    global _batch_depth
    _batch_depth += 1
    try:
        yield
    finally:
        _batch_depth -= 1
        if _batch_depth == 0:
            _flush_batch()


def _flush_batch() -> None:
    """Run all pending effects from the batch queue."""
    global _batch_queue
    iterations = 0
    while _batch_queue:
        if iterations > 100:
            log.error("Reactive batch exceeded 100 iterations — possible infinite loop")
            _batch_queue.clear()
            break
        # Take the current queue, clear it, run effects
        # (effects may produce new entries)
        pending = list(_batch_queue)
        _batch_queue.clear()
        for effect in pending:
            if not effect._disposed:
                effect.run()
        iterations += 1


# ── Utilities ──────────────────────────────────────────────────────

def untracked(fn: Callable[[], T]) -> T:
    """Execute fn without tracking any Signal reads.

        val = untracked(lambda: some_signal.get())  # no dependency created
    """
    token = _active_consumer.set(None)
    try:
        return fn()
    finally:
        _active_consumer.reset(token)


def dispose_all(*disposables: Effect | Computed) -> None:
    """Dispose multiple effects/computeds at once."""
    for d in disposables:
        d.dispose()
