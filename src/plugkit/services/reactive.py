"""Reactive state as a Cordis plugin — the one thing 1.0 had that Cordis lacks.

Cordis reloads a whole plugin when a *provider* changes identity (the epoch in
`fiber.py:_refresh`). That is the right granularity for "the database service was
replaced" and the wrong one for "a config value changed" — reloading a plugin to
observe a new timeout is a sledgehammer, and it is why 1.0 grew Signals.

The two compose rather than compete: a `Computed` or `Effect` created through
this service is registered as a fiber effect, so it is disposed when the plugin
that made it unloads. Nothing leaks, and nothing has to be torn down by hand.

    class Timeouts(Service):
        provide = "timeouts"
        def __cordis_init__(self):
            self.value = self.ctx.reactive.signal(30)

    def http(ctx, config=None):
        # re-runs on every change to the signal, disposed with this plugin
        ctx.reactive.effect(lambda: ctx.client.set_timeout(ctx.timeouts.value.get()))
    http.inject = ["timeouts", "client", "reactive"]

`signal()` is deliberately not an effect: a Signal is state, not a registration,
and it belongs to whoever holds it.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from ..signals import Computed, Effect, Signal, batch, is_stale
from ..cordis import Service

T = TypeVar("T")

__all__ = ["Signal", "Computed", "Effect", "batch", "is_stale", "ReactiveService"]


class ReactiveService(Service):
    """Provides `ctx.reactive`. Mount it once; every plugin can then use it.

    Because a service instance rebinds to the reading context, `self.ctx` in
    these methods is the *caller's* context — so `ctx.reactive.effect(...)`
    registers against the calling plugin's fiber, not this one's.
    """

    provide = "reactive"

    def signal(self, value: T) -> Signal[T]:
        """A new Signal. Not a registration — it has no owner and no disposer."""
        return Signal(value)

    def computed(self, fn: Callable[[], T]) -> Computed[T]:
        """A cached derivation, disposed when the calling plugin unloads."""
        holder: dict[str, Any] = {}

        def execute():
            holder["value"] = Computed(fn)
            return holder["value"].dispose

        self.ctx.effect(execute, "ctx.reactive.computed()")
        return holder["value"]

    def effect(
        self,
        fn: Callable[[], Any],
        *,
        lazy: bool = False,
        cancel_on_supersede: bool = False,
    ) -> Effect:
        """A reactive side effect, disposed when the calling plugin unloads.

        Runs once immediately (unless `lazy`), then again on every change to
        anything it read. Async bodies supersede rather than drop: see
        `_reactive_engine.Effect`.
        """
        holder: dict[str, Any] = {}

        def execute():
            holder["value"] = Effect(fn, lazy=lazy, cancel_on_supersede=cancel_on_supersede)
            return holder["value"].dispose

        self.ctx.effect(execute, "ctx.reactive.effect()")
        return holder["value"]

    def batch(self):
        """Coalesce several signal writes into one downstream flush."""
        return batch()
