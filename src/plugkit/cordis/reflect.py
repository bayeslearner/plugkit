"""Service registry reflection: provide, get, set, accessor, mixin."""

from __future__ import annotations

import asyncio
import types
from typing import Any, Optional

from .fiber import FiberState
from .traceable import get_traceable
from .utils import unique_symbol


class Impl:
    __slots__ = ("name", "fiber", "value", "check")

    def __init__(self, name: str, fiber, value: Any, check):
        self.name = name
        self.fiber = fiber
        self.value = value
        self.check = check


class ReflectService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.__cordis_tracker__ = {"property": "ctx", "noShadow": True}
        self.store: dict = {}
        self.props: dict = {}

        self.mixin("reflect", ["get", "set", "provide", "accessor", "mixin"])
        self.mixin("fiber", ["runtime", "effect"])
        self.mixin("registry", ["inject", "plugin"])
        self.mixin("events", ["on", "once", "parallel", "emit", "serial", "bail", "waterfall"])

    def get(self, name: str, strict: bool = True):
        impl = self._get_impl(name, strict)
        if impl is None:
            return None
        return get_traceable(self.ctx, impl.value)

    def _get_impl(self, name: str, strict: bool = True) -> Optional[Impl]:
        key = self.ctx._effective_isolate().get(name)
        impl = self.store.get(key) if key is not None else None
        if impl is None:
            return None
        if strict and impl.fiber.state != FiberState.ACTIVE:
            return None
        return impl

    def set(self, name: str, value: Any, error=None) -> bool:
        key = self.ctx._effective_isolate().get(name)
        impl = self.store.get(key) if key is not None else None
        if impl is None:
            raise AttributeError(f'cannot set property "{name}" without provide')
        if impl.fiber is not self.ctx.fiber:
            raise AttributeError(f'cannot set property "{name}" in multiple fibers')
        impl.value = value
        return True

    def provide(self, name: str, value: Any = None, check=None):
        self_ = self

        def effect():
            if name in self_.props and self_.props[name]["type"] != "service":
                raise AttributeError(f'property "{name}" is already declared as {self_.props[name]["type"]}')
            self_.props[name] = {"type": "service"}

            self_.ctx.root.__dict__["_isolate"].setdefault(name, unique_symbol(name))
            key = self_.ctx._effective_isolate().get(name)
            impl = Impl(name, self_.ctx.fiber, value, check)

            existing = self_.store.get(key)
            if existing is not None:
                raise AttributeError(f'service "{name}" has been registered at <{existing.fiber.name}>')
            self_.store[key] = impl
            if self_.ctx.fiber.store is not None:
                self_.ctx.fiber.store[name] = impl
            if self_.ctx.fiber.state == FiberState.ACTIVE:
                self_.notify([name])

            def disposer():
                # JS: an async disposer runs its synchronous prefix at call time
                del self_.store[key]
                fibers = self_.notify([name])

                async def rest():
                    await asyncio.gather(*(f.await_() for f in fibers), return_exceptions=True)
                    if self_.ctx.fiber.store is not None:
                        self_.ctx.fiber.store.pop(name, None)

                return rest()

            return disposer

        return self.ctx.fiber.effect(effect, f'ctx.provide("{name}")')

    def notify(self, names: list, filter_=None):
        if filter_ is None:
            isolate = self.ctx._effective_isolate()

            def filter_(ctx, name):
                return ctx._effective_isolate().get(name) is isolate.get(name)

        fibers = []
        for runtime in self.ctx.registry.values():
            for fiber in runtime.fibers:
                has_update = False
                for name in names:
                    if name not in fiber.inject:
                        continue
                    if not filter_(fiber.ctx, name):
                        continue
                    has_update = True
                    fiber._check_impl(name)
                if not has_update:
                    continue
                fiber._refresh()
                fibers.append(fiber)

        for name in names:
            def make_filter(name):
                def f(target):
                    return filter_(target, name)

                return f

            self_ctx = self.ctx.extend({"_filter": make_filter(name)})
            impl = self._get_impl(name, False)
            self.ctx.events.emit(self_ctx, "internal/service", name, impl.value if impl is not None else None)
        return fibers

    def accessor(self, name: str, options: dict):
        self_ = self

        def effect():
            if name in self_.props:
                raise AttributeError(f'property "{name}" is already declared as {self_.props[name]["type"]}')
            self_.props[name] = {"type": "accessor", **options}

            def disposer():
                del self_.props[name]

            return disposer

        return self.ctx.fiber.effect(effect, f'ctx.accessor("{name}")')

    def mixin(self, source, mixins):
        self_ = self

        def get_target(this_ctx, error):
            # JS `ctx[source]` — objects coerce to a string key that is
            # guaranteed to be absent, which surfaces as a reflect error.
            key = source if isinstance(source, str) else str(source)
            return getattr(this_ctx, key)

        entries = mixins if isinstance(mixins, list) else list(mixins.items())
        if isinstance(mixins, list):
            entries = [(key, key) for key in mixins]
        else:
            entries = list(mixins.items())

        def effect_gen():
            for key, value in entries:

                def make_get(key):
                    def get(this_ctx, receiver, error):
                        service = get_target(this_ctx, error)
                        if service is None:
                            return None
                        inner = getattr(service, key, None)
                        if inner is None:
                            return None
                        if callable(inner) and not isinstance(inner, types.MethodType):
                            from .traceable import Traceable

                            if isinstance(service, Traceable):
                                # the shadow wrapper already binds `this`
                                return inner
                            # JS: `value.bind(mixin ?? service)` — the member
                            # call binds `this` to the receiver.
                            target = receiver if receiver is not None else service
                            return types.MethodType(inner, target)
                        return inner

                    return get

                def make_set(key):
                    def set_(this_ctx, value_, receiver, error):
                        service = get_target(this_ctx, error)
                        setattr(service, key, value_)
                        return True

                    return set_

                yield self_.accessor(value, {"get": make_get(key), "set": make_set(key)})

        if isinstance(source, str):
            label = f'ctx.mixin("{source}")'
        else:
            label = "ctx.mixin(instance)"
        return self.ctx.fiber.effect(effect_gen, label)

    def trace(self, value: Any):
        return get_traceable(self.ctx, value)

    def bind(self, callback):
        """Make a listener's arguments traceable against the owning context.

        Every argument is traced. The dispatch carrier is not an argument —
        it is ambient (`utils.this_()`), so listener arity matches Cordis.
        """
        def wrapper(*args):
            return callback(*(get_traceable(self.ctx, arg) for arg in args))

        return wrapper
