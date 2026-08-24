"""Context — the entry point of every cordis application."""

from __future__ import annotations

from typing import Any, Optional

from .fiber import Fiber
from .traceable import get_traceable
from .utils import ChainedDict, symbols, unique_symbol


class Context:
    """A dependency-injection context.

    Attribute access mirrors Cordis's reflect semantics:
    - properties defined anywhere in the extend chain resolve directly;
    - undeclared reads raise `AttributeError` unless a service was provided;
    - undeclared writes raise `AttributeError` inside plugins.
    """

    # JS `Context.effect/filter/isolate/intercept` static symbols are
    # available through the `symbols` namespace instead: Python class
    # attributes would shadow instance attribute resolution.

    _parent: Optional["Context"] = None

    @staticmethod
    def is_(value: Any) -> bool:
        """Mirror JS `Context.is(value)` — `is` is a Python keyword."""
        return bool(getattr(value, "_is_context", False))

    def __init__(self) -> None:
        d = self.__dict__
        d["_parent"] = None
        d["_isolate"] = ChainedDict()
        d["_intercept"] = ChainedDict()
        d["_is_context"] = True
        d["root"] = self
        d["baseUrl"] = None
        from .reflect import ReflectService
        from .registry import RegistryService
        from .events import EventsService
        from .logger import LoggerService

        d["fiber"] = Fiber(self, {}, {}, None, lambda: [])
        d["reflect"] = ReflectService(self)
        d["registry"] = RegistryService(self)
        d["events"] = EventsService(self)
        d["logger"] = LoggerService(self)
        self.fiber._disposables.clear()

    def __repr__(self) -> str:
        return f"Context <{self.fiber.name}>"

    # ------------------------------------------------------------------
    # extend / isolate / intercept
    # ------------------------------------------------------------------

    def extend(self, meta=None) -> "Context":
        cls = type(self)
        base = object.__new__(cls)
        base.__dict__["_parent"] = self
        if meta is not None:
            items = meta.items() if isinstance(meta, dict) else meta.__dict__.items()
            for key, value in items:
                base.__dict__[key] = value
        if "_shadow" in self.__dict__:
            ctx = object.__new__(cls)
            ctx.__dict__["_parent"] = base
            ctx.__dict__["_shadow"] = self.__dict__["_shadow"]
            return ctx
        return base

    def isolate(self, name: str, label=None) -> "Context":
        # JS: `Object.create(this[isolate])` — a live chain, not a copy
        shadow = ChainedDict({name: label if label is not None else unique_symbol(name)}, parent=self._effective_isolate())
        return self.extend({"_isolate": shadow})

    def intercept(self, name: str, config: Any) -> "Context":
        intercept = ChainedDict({name: config}, parent=self._effective_intercept())
        return self.extend({"_intercept": intercept})

    def _effective_isolate(self) -> dict:
        obj = self
        while obj is not None:
            if "_isolate" in obj.__dict__:
                return obj.__dict__["_isolate"]
            obj = obj.__dict__.get("_parent")
        return {}

    def _effective_intercept(self) -> dict:
        obj = self
        while obj is not None:
            if "_intercept" in obj.__dict__:
                return obj.__dict__["_intercept"]
            obj = obj.__dict__.get("_parent")
        return {}

    # ------------------------------------------------------------------
    # attribute interception
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in ("prototype", "then", "constructor"):
            obj = self
            while obj is not None:
                if name in obj.__dict__:
                    return obj.__dict__[name]
                obj = obj.__dict__.get("_parent")
            if name in ("prototype", "then", "constructor"):
                return None
            raise AttributeError(name)

        obj = self
        while obj is not None:
            if name in obj.__dict__:
                value = obj.__dict__[name]
                if obj.__dict__.get("_parent") is not None:
                    # own property of an extended context — no tracing
                    return value
                return get_traceable(self, value)
            obj = obj.__dict__.get("_parent")

        return self._reflect_get(name)

    # ── subscript access ──────────────────────────────────────────────
    #
    # `ctx["database"]` is the same lookup as `ctx.database`, spelled so the
    # string is visible. Services are found by name, never by type, and the
    # attribute form hides that well enough that people read it as type-based
    # injection. This form cannot be misread, and it is the only way to reach a
    # service whose name is not a valid Python identifier.
    #
    # Identical rules: same isolation scope, same refusal to read a service the
    # current fiber did not inject.

    def __getitem__(self, name: str) -> Any:
        if not isinstance(name, str):
            raise TypeError(f"a service name must be a string, got {name!r}")
        try:
            return getattr(self, name)
        except AttributeError as exc:
            raise KeyError(str(exc)) from exc

    def __setitem__(self, name: str, value: Any) -> None:
        """Update an already-provided service. This does not register one.

        Registering is `ctx.provide(name, value)`, which hands back the disposer
        that makes it reversible. A bare assignment has nowhere to put a
        disposer, so it is refused — the same reasoning that refuses a read of a
        service the fiber did not inject.
        """
        if not isinstance(name, str):
            raise TypeError(f"a service name must be a string, got {name!r}")
        setattr(self, name, value)

    def __contains__(self, name: object) -> bool:
        """True when `name` resolves to a service *this* fiber may read.

        False rather than raising for a service that exists but was not
        injected — `in` answers "can I use this", which is the question a
        caller is actually asking.
        """
        if not isinstance(name, str):
            return False
        try:
            return getattr(self, name) is not None
        except (AttributeError, KeyError):
            return False

    def _reflect_get(self, name: str, receiver: Any = None) -> Any:
        error = AttributeError(f'cannot get property "{name}" without inject')
        prop = self.reflect.props.get(name)
        if prop is not None and prop["type"] == "accessor":
            return prop["get"](self, receiver, error)
        if self.fiber.runtime is None:
            return self.reflect.get(name, False)
        return self.events.waterfall(self, "internal/get", name, error, lambda *args: self._default_get(name, error))

    def _default_get(self, name: str, error: AttributeError) -> Any:
        key = self.root.__dict__["_isolate"].get(name)
        shadow = getattr(self, "_shadow", None)
        fiber = (shadow if shadow is not None else self).fiber
        while True:
            impl = fiber.store.get(name) if fiber.store is not None else None
            if impl is not None:
                return get_traceable(self, impl.value)
            if name in fiber.inject:
                raise AttributeError(f'cannot get required service "{name}" in inactive context')
            if fiber.runtime is None:
                raise error
            if fiber.parent._effective_isolate().get(name) is not key:
                raise error
            fiber = fiber.parent.fiber

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") or name in ("prototype", "then", "constructor"):
            object.__setattr__(self, name, value)
            return

        obj = self
        while True:
            if name in obj.__dict__:
                obj.__dict__[name] = value
                return
            parent = obj.__dict__.get("_parent")
            if parent is None:
                break
            obj = parent

        self._reflect_set(name, value, set_on_root=obj)

    def _reflect_set(self, name: str, value: Any, receiver: Any = None, set_on_root: Optional["Context"] = None) -> Any:
        error = AttributeError(f'cannot set property "{name}" without provide')
        prop = self.reflect.props.get(name)
        if prop is None:
            if self.fiber.runtime is None:
                if set_on_root is not None:
                    set_on_root.__dict__[name] = value
                else:
                    object.__setattr__(self, name, value)
                return
            raise error
        if prop["type"] == "accessor":
            setter = prop.get("set")
            if setter is None:
                return False
            return setter(self, value, receiver, error)
        return self.events.waterfall(self, "internal/set", name, value, error, lambda *args: self.reflect.set(name, value, error))
