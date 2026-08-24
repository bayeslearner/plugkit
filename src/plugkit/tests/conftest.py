"""Shared helpers for the cordis test suite."""

import asyncio

import pytest

from plugkit.cordis import Context, Service


class Mock:
    """A minimal `mock.fn()` stand-in."""

    def __init__(self, fn=None):
        import inspect

        self.calls = []
        self._impl = fn
        # node's `mock.fn()` is constructible: a mock wrapping a class
        # constructs it when the fiber instantiates the plugin
        self.__cordis_is_constructor__ = inspect.isclass(fn)

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self._impl is not None:
            return self._impl(*args, **kwargs)
        return None

    def reset_calls(self):
        self.calls.clear()

    def mock_implementation(self, fn):
        self._impl = fn


async def sleep(ms: int = 0):
    """Flush pending event-loop work (JS `sleep(0)` settles all microtasks)."""
    for _ in range(20):
        await asyncio.sleep(0)


event = "custom-event"


class Session:
    """A filterable event dispatch target."""

    def __init__(self, flag: bool):
        self.flag = flag

    def _filter(self, context) -> bool:
        filter_fn = getattr(context, "filter", None)
        if not filter_fn:
            return True
        return filter_fn(self)


class Filter:
    """Used with `ctx.extend(Filter(flag))` to add a `filter` property."""

    def __init__(self, flag: bool):
        self.flag = flag
        # JS: instance field — must be an own attribute for `extend()` to copy it
        self.filter = lambda session: session.flag == self.flag


class Counter:
    """A tracked object with an `increase()` effect."""

    def __init__(self, ctx: Context):
        self.__cordis_tracker__ = {"associate": "counter", "property": "ctx"}
        self.ctx = ctx
        self.value = 0

    def increase(self):
        self_ = self

        def effect():
            self_.value += 1

            def dispose():
                self_.value -= 1

            return dispose

        return self.ctx.effect(effect)


def get_hook_snapshot(ctx: Context) -> dict:
    result = {}
    for name, callbacks in ctx.events._hooks.items():
        if callbacks:
            result[name] = len(callbacks)
    return result
