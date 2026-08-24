"""Plugin registry and the `Inject` decorator."""

from __future__ import annotations

from typing import Any, Optional

from .fiber import Fiber, Runtime
from .utils import build_outer_stack, symbol

_INJECT_PARENT = "__cordis_parent_inject__"


def _normalize_inject(inject):
    if inject is None:
        return {}
    if isinstance(inject, (list, tuple, set)):
        return {name: None for name in inject}
    return dict(inject)


class Inject:
    """`@Inject('foo')` / `@Inject('foo', config)` decorator."""

    def __new__(cls, name: str, config: Any = None):
        def decorator(target):
            if isinstance(target, type):
                inject = _normalize_inject(getattr(target, "inject", None))
                inject[_INJECT_PARENT] = getattr(target, "inject", None)
                inject[name] = config
                target.inject = inject
                return target
            if callable(target):
                inject = dict(getattr(target, "__cordis_inject__", None) or {})
                inject[name] = config
                target.__cordis_inject__ = inject
                return target
            raise TypeError("@Inject() can only be used on class or class methods")

        return decorator

    @staticmethod
    def resolve(inject, result: Optional[dict] = None):
        if result is None:
            result = {}
        if not inject:
            return result
        if isinstance(inject, (list, tuple, set)):
            for name in inject:
                result[name] = None
            return result
        parent = inject.get(_INJECT_PARENT)
        if parent is not None:
            Inject.resolve(parent, result)
        for name, config in inject.items():
            if name == _INJECT_PARENT:
                continue
            result[name] = config if config is not None else None
        return result


class RegistryService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.__cordis_tracker__ = {"property": "ctx", "noShadow": True}
        self._counter = 0
        self._internal: dict = {}

    @property
    def counter(self) -> int:
        self._counter += 1
        return self._counter

    @property
    def size(self) -> int:
        return len(self._internal)

    def resolve(self, plugin):
        try:
            if callable(plugin):
                return plugin
            if isinstance(plugin, dict):
                apply = plugin.get("apply")
            else:
                apply = getattr(plugin, "apply", None)
            if callable(apply):
                return apply
        except Exception:
            pass
        return None

    def get(self, plugin):
        key = self.resolve(plugin)
        return self._internal.get(key) if key is not None else None

    def has(self, plugin) -> bool:
        key = self.resolve(plugin)
        return key is not None and key in self._internal

    def delete(self, plugin):
        key = self.resolve(plugin)
        runtime = self._internal.get(key) if key is not None else None
        if runtime is None:
            return None
        del self._internal[key]
        from .fiber import _is_awaitable, _schedule

        for fiber in list(runtime.fibers):
            result = fiber.dispose()
            if _is_awaitable(result):
                _schedule(result)
        return runtime

    def keys(self):
        return self._internal.keys()

    def values(self):
        return self._internal.values()

    def entries(self):
        return self._internal.items()

    def for_each(self, callback):
        for key, value in self._internal.items():
            callback(value, key)

    forEach = for_each

    def inject(self, inject, callback):
        return self.plugin({"inject": inject, "apply": callback, "name": getattr(callback, "__name__", None)})

    def plugin(self, plugin, config: Any = None, get_outer_stack=None):
        callback = self.resolve(plugin)
        if callback is None:
            raise TypeError(
                f'invalid plugin, expect function or object with an "apply" method, received {type(plugin).__name__}'
            )
        self.ctx.fiber.assert_active()

        runtime = self._internal.get(callback)
        if runtime is None:
            if isinstance(plugin, dict):
                name = plugin.get("name")
            else:
                name = getattr(plugin, "name", None)
            if name is None:
                name = getattr(plugin, "__name__", None)
            if name in ("apply", "<lambda>"):
                name = None
            from .utils import DisposableList

            runtime = Runtime(name=name, callback=callback, fibers=DisposableList(), Config=getattr(plugin, "Config", None))
            self._internal[callback] = runtime

        inject = plugin.get("inject") if isinstance(plugin, dict) else getattr(plugin, "inject", None)
        if get_outer_stack is None:
            get_outer_stack = build_outer_stack()
        fiber = Fiber(self.ctx, config, Inject.resolve(inject), runtime, get_outer_stack)
        return fiber
