"""Extension points — many plugins filling one named role.

The kernel gives you one half of this already. `ctx.on(name, listener)` is a
named role that any number of plugins contribute to, each contribution owned by
the contributing fiber, and the five dispatch modes run them. What it does not
give you is the ability to *read* the contributions: `_hooks` is private, the
chain is anonymous, and a consumer cannot be woken when the set changes.

So every registry writes those four things itself. In DeepSeek Harness eleven
packages define their own `register()`; in this project `services/tools.py` did
it three times in one file. The shape is always the same:

    def register(self, thing):
        collection = self._collection
        def execute():
            collection.append(thing)
            return lambda: collection.remove(thing)
        return self.ctx.effect(execute, "label")

This service is that shape, once.

    def admin(ctx, config=None):
        ctx.points.add("http.routes", handler, key="/admin", order=10)

    admin.inject = ["points"]

`add` returns a disposer registered against **`admin`'s** fiber, not this
service's, so unloading `admin` removes the route. The caller writes no teardown.

A point is never declared. It exists when something is contributed to it, and
stops existing when the last contribution goes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..cordis import Service
from ..signals import Effect, Signal

__all__ = ["PointsService", "Contribution"]


@dataclass(frozen=True)
class Contribution:
    """One plugin's entry in a point."""

    value: Any
    key: str | None = None
    order: float = 0.0
    props: dict = field(default_factory=dict)
    #: Registration sequence, the tiebreaker for equal `order`.
    seq: int = 0

    def matches(self, props: dict) -> bool:
        """Whether every asked-for property is present and equal.

        A property whose contributed value is a list or tuple matches if the
        asked-for value is *in* it, so `methods=["GET", "POST"]` answers
        `where(point, methods="GET")`.
        """
        for name, wanted in props.items():
            if name not in self.props:
                return False
            held = self.props[name]
            if isinstance(held, (list, tuple, set, frozenset)):
                if wanted not in held:
                    return False
            elif held != wanted:
                return False
        return True


class PointsService(Service):
    """Provides `ctx.points`.

    An ordinary plugin with no privileged status — mount it like any other::

        await root.plugin(PointsService)

    Isolation needs nothing from this class. `ctx.isolate("points")` gives a
    subtree its own instance with its own storage, so two subtrees hold
    different contributions under the same point name.
    """

    provide = "points"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        self._points: dict[str, list[Contribution]] = {}
        self._signals: dict[str, Signal] = {}
        self._seq = 0

    # ── contributing ──────────────────────────────────────────────────

    def add(
        self,
        point: str,
        value: Any,
        *,
        key: str | None = None,
        order: float = 0.0,
        unique: bool = False,
        **props: Any,
    ) -> Callable[[], Any]:
        """Contribute `value` to `point`. Returns a disposer.

        The disposer is registered as an effect of the *calling* plugin, so the
        contribution disappears when that plugin unloads.

        :param key: an identifier for `get()`. Optional; a point can hold
            anonymous contributions.
        :param order: sort position, ascending. Ties break on registration
            order, so equal-`order` contributions run in the order they arrived.
        :param unique: raise if `key` is already taken in this point. Declared
            per contribution because a point is never declared.
        :param props: arbitrary properties, matched by `where()`.
        """
        if not isinstance(point, str) or not point:
            raise TypeError(f"a point name must be a non-empty string, got {point!r}")
        if key is not None and not isinstance(key, str):
            raise TypeError(f"a contribution key must be a string or None, got {key!r}")
        if unique and key is None:
            raise TypeError("unique=True needs a key to be unique on")

        def execute():
            entries = self._points.setdefault(point, [])
            if unique and any(e.key == key for e in entries):
                raise ValueError(f"{point!r} already has a contribution keyed {key!r}")

            self._seq += 1
            entry = Contribution(
                value=value, key=key, order=order, props=dict(props), seq=self._seq
            )
            entries.append(entry)
            self._wake(point)

            def remove():
                held = self._points.get(point)
                if held is None:
                    return
                # by identity: a value contributed twice removes only this entry
                for index, candidate in enumerate(held):
                    if candidate is entry:
                        held.pop(index)
                        break
                if not held:
                    del self._points[point]
                self._wake(point)

            return remove

        label = f"ctx.points.add({point!r}" + (f", key={key!r})" if key else ")")
        return self.ctx.effect(execute, label)

    # ── reading ───────────────────────────────────────────────────────

    def entries(self, point: str) -> list[Contribution]:
        """Every contribution to `point`, in order. Reading tracks the point."""
        self._track(point)
        return sorted(self._points.get(point, ()), key=lambda e: (e.order, e.seq))

    def all(self, point: str) -> list[Any]:
        """Every contributed value, in order."""
        return [entry.value for entry in self.entries(point)]

    def get(self, point: str, key: str) -> Any | None:
        """The value contributed under `key`, or None.

        With several contributions under one key — possible unless a contributor
        asked for `unique` — the last one added wins, matching `last()`.

        Resolved on `seq`, not on position in `entries()`. `entries()` sorts by
        `order` first, so taking the last match there returns the highest
        `order`, which is the *earlier* arrival whenever the newer contribution
        carries a lower `order`. `get` and `last` are both "the current one" and
        must not disagree.
        """
        matches = [entry for entry in self.entries(point) if entry.key == key]
        return max(matches, key=lambda e: e.seq).value if matches else None

    def where(self, point: str, **props: Any) -> list[Any]:
        """Contributed values whose properties match all of `props`."""
        return [entry.value for entry in self.entries(point) if entry.matches(props)]

    def last(self, point: str) -> Any | None:
        """The most recently added value, or None.

        This is the property a "current handler" slot wants. Holding one
        variable and restoring a saved value on dispose is only correct if
        plugins unload in reverse mount order; removing by identity from a list
        has no such assumption.
        """
        entries = self.entries(point)
        return max(entries, key=lambda e: e.seq).value if entries else None

    def names(self) -> list[str]:
        """Every point holding at least one contribution."""
        return sorted(self._points)

    def count(self, point: str) -> int:
        self._track(point)
        return len(self._points.get(point, ()))

    def has(self, point: str) -> bool:
        """Whether `point` holds at least one contribution.

        Named rather than `__contains__`: Python looks dunders up on the type,
        so they bypass the `Traceable.__getattr__` that rebinds a service onto
        the caller's view. `"p" in ctx.points` would raise where
        `ctx.points.has("p")` works, and a method that behaves differently
        depending on whether the service was injected is worse than no method.
        """
        self._track(point)
        return bool(self._points.get(point))

    # ── reacting ──────────────────────────────────────────────────────

    def on_change(self, point: str, callback: Callable[[], Any]) -> Callable[[], Any]:
        """Run `callback` when the contributions to `point` change.

        Returns a disposer owned by the *calling* plugin, so a consumer that
        unloads stops being called. `callback` does not run on registration —
        the caller has just read the current set, and an effect that fires once
        at registration is the behaviour `ctx.reactive.effect` already provides.
        """
        signal = self._signal(point)

        def execute():
            first = True

            def run():
                nonlocal first
                signal.get()  # the tracking read; must happen on every run
                if first:
                    first = False
                    return
                callback()

            return Effect(run).dispose

        return self.ctx.effect(execute, f"ctx.points.on_change({point!r})")

    # ── internals ─────────────────────────────────────────────────────

    def _signal(self, point: str) -> Signal:
        signal = self._signals.get(point)
        if signal is None:
            signal = Signal(0)
            self._signals[point] = signal
        return signal

    def _track(self, point: str) -> None:
        """Read the point's Signal, so a reactive effect that called us re-runs.

        One Signal per point: contributing to `http.routes` must not wake a
        consumer reading `tools`.
        """
        self._signal(point).get()

    def _wake(self, point: str) -> None:
        signal = self._signals.get(point)
        if signal is not None:
            signal.set(signal.peek() + 1)
