"""Event dispatch: emit, parallel, serial, bail, waterfall."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from .fiber import FiberState
from .utils import DisposableList, acall_listener, call_listener, is_object, this_


class AggregateError(ExceptionGroup):
    """Python's answer to `AggregateError` — raised by `ctx.parallel()`."""


def is_bailed(value: Any) -> bool:
    return value is not None and value is not False


def _start_coroutine(coro):
    """Start an async dispatch eagerly when a loop is running (JS async
    functions start immediately); otherwise return the bare coroutine."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return coro
    return asyncio.ensure_future(coro)


class EventsService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.__cordis_tracker__ = {"property": "ctx", "noShadow": True}
        self._hooks: dict[str, list] = {}

        def internal_listener(name, listener, options):
            this_ctx = this_()
            if name == "internal/update" and not options.get("global"):
                hooks = this_ctx.fiber._hooks.get("internal/update")
                if hooks is None:
                    hooks = DisposableList()
                    this_ctx.fiber._hooks["internal/update"] = hooks
                return hooks.push(listener)

        self.on("internal/listener", internal_listener)

        def internal_update(config, no_save, _next):
            fiber = this_()  # the carrier of this dispatch is the fiber
            cbs = list(fiber._hooks.get("internal/update") or [])

            def next_():
                cb = cbs.pop(0) if cbs else _next
                if cb is _next:
                    return _next()
                return call_listener(cb, fiber, (config, no_save, next_))

            return next_()

        self.on("internal/update", internal_update, {"global": True, "prepend": True})

    def _resolve(self, type_: str, args: list):
        if len(args) and (is_object(args[0]) or callable(args[0])):
            this_arg = args.pop(0)
        else:
            this_arg = None
        name = args.pop(0)
        if not name.startswith("internal/") and self._hooks.get("internal/dispatch"):
            self.emit("internal/dispatch", type_, name, args, this_arg)
        filter_ = getattr(this_arg, "_filter", None) if this_arg is not None else None
        callbacks = []
        for hook in self._hooks.get(name) or []:
            if hook["global"] or filter_ is None or filter_(hook["ctx"]):
                callbacks.append(hook["callback"])
        return this_arg, callbacks

    def dispatch(self, type_: str, args: list):
        """Deprecated — returns bound callbacks."""
        this_arg, callbacks = self._resolve(type_, list(args))
        return [cb.__get__(this_arg) for cb in callbacks]

    def parallel(self, *args):
        args = list(args)
        this_arg, callbacks = self._resolve("emit", args)

        async def apply(callback):
            return await acall_listener(callback, this_arg, args)

        async def run():
            results = await asyncio.gather(*(apply(cb) for cb in callbacks), return_exceptions=True)
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                raise AggregateError("multiple listeners failed", errors)

        return _start_coroutine(run())

    def emit(self, *args):
        args = list(args)
        this_arg, callbacks = self._resolve("emit", args)
        for callback in callbacks:
            call_listener(callback, this_arg, args)

    def serial(self, *args):
        args = list(args)
        this_arg, callbacks = self._resolve("serial", args)

        async def run():
            for callback in callbacks:
                result = await acall_listener(callback, this_arg, args)
                if is_bailed(result):
                    return result

        return _start_coroutine(run())

    def bail(self, *args):
        args = list(args)
        this_arg, callbacks = self._resolve("bail", args)
        for callback in callbacks:
            result = call_listener(callback, this_arg, args)
            if is_bailed(result):
                return result

    def waterfall(self, *args):
        args = list(args)
        this_arg, callbacks = self._resolve("waterfall", args)
        inner = args.pop()

        def next_():
            if callbacks:
                callback = callbacks.pop(0)
                return call_listener(callback, this_arg, args)
            # The innermost default is the dispatching service's own behaviour.
            # TS calls it with the full arg list and lets it ignore the extras;
            # Python has no such tolerance, and every real Cordis default is
            # nullary (`() => ({ kind: 'allow' })`), so call it with no args.
            return inner()

        args.append(next_)
        return next_()

    def register(self, label: str, hooks: list, callback, options: dict):
        def effect():
            hook = {"ctx": self.ctx, "callback": callback, "prepend": options.get("prepend", False), "global": options.get("global", False)}
            if options.get("prepend"):
                hooks.insert(0, hook)
            else:
                hooks.append(hook)
            return lambda: self.unregister(hooks, callback)

        return self.ctx.fiber.effect(effect, label)

    def unregister(self, hooks: list, callback) -> bool:
        for index, hook in enumerate(hooks):
            if hook["callback"] is callback:
                hooks.pop(index)
                return True
        return False

    def on(self, name, listener, options=None):
        if not isinstance(options, dict):
            options = {"prepend": bool(options)}
        options = dict(options)

        self.ctx.fiber.assert_active()
        listener = self.ctx.reflect.bind(listener)
        result = self.bail(self.ctx, "internal/listener", name, listener, options)
        if result:
            return result

        hooks = self._hooks.get(name)
        if hooks is None:
            hooks = []
            self._hooks[name] = hooks
        if isinstance(name, str):
            label = f"ctx.on({json.dumps(name)})"
        else:
            label = f"ctx.on({name})"
        return self.register(label, hooks, listener, options)

    def once(self, name, listener, options=None):
        dispose = None

        def wrapper(*args):
            dispose()
            return listener(*args)

        dispose = self.on(name, wrapper, options)
        return dispose
