"""Is dependency injection itself a plugin? Mostly yes. Here is the proof.

The question is where the line falls between "kernel" and "plugin", and it falls
in a specific place worth knowing.

**Below the line — the kernel, and it cannot be a plugin.** Two things:

  1. *Resolution*  — `ctx.database` finding the object registered under that name
  2. *Lifetime*   — deciding when your plugin runs, and unwinding it when a
                    dependency leaves

You cannot mount a plugin that provides the ability to mount plugins. That is a
bootstrap problem, not a design choice. It lives in `cordis/` — 4,298 lines.

**Above the line — everything people usually mean by "a DI framework".** Object
construction strategy, scopes, lifetimes-per-object, config binding, the wiring
declaration format. All of that is policy, and policy is a plugin.

`binding.provide()` is one such policy — 378 lines, and it touches the kernel
through exactly three methods: `ctx.provide`, `ctx.inject`, `ctx.effect`. Anything
that can call those three can be a rival policy, mounted alongside, with no
kernel change.

This file is a rival policy. `provide_factory` builds a *new* instance per call
instead of one shared instance, which is `dependency-injector`'s `Factory` versus
its `Singleton`. Nothing was added to the kernel to allow it.

    from plugkit import Context
    from plugkit.examples.alternative_binding import provide_factory

    await root.plugin(provide_factory(RequestScope, "request"))
    a = root.request()          # a fresh RequestScope
    b = root.request()          # a different one
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..binding import _resolve_needs

__all__ = ["provide_factory"]


class _Factory:
    """What gets registered: a callable returning a new component each call."""

    def __init__(self, factory: Callable[..., Any], kwargs: dict):
        self._factory = factory
        self._kwargs = kwargs
        self.instances: list[Any] = []

    def __call__(self, **overrides: Any) -> Any:
        instance = self._factory(**{**self._kwargs, **overrides})
        self.instances.append(instance)
        return instance

    def close_all(self) -> None:
        for instance in self.instances:
            closer = getattr(instance, "close", None)
            if callable(closer):
                closer()
        self.instances.clear()


def provide_factory(
    factory: Callable[..., Any],
    service_name: str,
    *,
    needs: Any = None,
    extra: Mapping[str, Any] | None = None,
):
    """Register a *factory* rather than an instance. A rival construction policy.

    `provide()` registers one shared object. This registers a callable, so every
    caller gets its own — the shape you want for per-request or per-tenant state.

    Note what this does NOT need: no kernel change, no new event, no hook. It is
    an ordinary plugin, because construction policy was always above the line.
    """
    wiring = _resolve_needs(needs)

    def apply(ctx, plugin_config=None):
        kwargs = dict(extra or {})
        for kwarg, service in wiring.items():
            kwargs[kwarg] = getattr(ctx, service)

        maker = _Factory(factory, kwargs)
        ctx.provide(service_name, maker)

        # Everything this policy created is still owned by this fiber, so a
        # per-call policy loses none of the guarantee a per-instance one has.
        return maker.close_all

    return {
        "name": f"{service_name}-factory",
        "inject": sorted(set(wiring.values())),
        "apply": apply,
    }
