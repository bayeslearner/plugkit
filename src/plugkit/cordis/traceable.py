"""Traceable wrappers — the Python equivalent of Cordis's JS proxies.

In JavaScript Cordis, tracked services are wrapped in `Proxy` objects so that
`service.ctx` resolves to the *caller's* context and method calls run with
caller-context semantics.  Python implements the same behavior with explicit
wrapper classes instead of proxies.
"""

from __future__ import annotations

import functools
import inspect
import types
from typing import Any, Optional

from .utils import symbols


def _strip_shadow(ctx):
    """JS: `caller = ctx[shadow] ?? ctx; if (ctx[shadow]) ctx = proto(ctx)`."""
    if "_shadow" in ctx.__dict__:
        return ctx.__dict__["_shadow"], ctx.__dict__["_parent"]
    return None, ctx


def get_traceable(ctx, value: Any) -> Any:
    """JS `getTraceable(ctx, value)`."""
    if not isinstance(value, object):
        return value
    if isinstance(value, (bool, int, float, complex, str, bytes, bytearray)):
        return value
    if isinstance(value, Traceable):
        return value
    if hasattr(value, "__dict__") and "_shadow" in value.__dict__:
        return value.__dict__["_parent"]
    tracker = getattr(value, symbols.tracker, None)
    if tracker is None:
        return value
    return Traceable(ctx, value, tracker)


class ShadowView:
    """The `this` seen inside methods of a tracked service.

    `self.ctx` returns the shadow context (caller context + shadow marker);
    every other attribute resolves against the underlying service.
    """

    __slots__ = ("_traceable", "_shadow_ctx")

    def __init__(self, traceable: "Traceable", shadow_ctx):
        object.__setattr__(self, "_traceable", traceable)
        object.__setattr__(self, "_shadow_ctx", shadow_ctx)

    def __getattr__(self, name: str) -> Any:
        if name == "ctx":
            return self._shadow_ctx
        if name == symbols.caller:
            return self._traceable._cordis_caller
        if name == symbols.original:
            return self._traceable._cordis_value
        if name.startswith("__cordis_"):
            inner = getattr(self._traceable._cordis_value, name, None)
            if callable(inner):
                func = getattr(inner, "__func__", inner)
                return types.MethodType(func, self)
            return inner
        return getattr(self._traceable, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_traceable", "_shadow_ctx"):
            object.__setattr__(self, name, value)
            return
        setattr(self._traceable, name, value)

    def __call__(self, *args, **kwargs):
        return self._traceable(*args, **kwargs)

    @property
    def __class__(self):
        return type(self._traceable._cordis_value)

    def __repr__(self) -> str:
        return repr(self._traceable._cordis_value)


class Traceable:
    """Wraps a tracked service value with caller-context semantics."""

    __slots__ = ("_cordis_value", "_cordis_ctx", "_cordis_caller", "_cordis_tracker")

    def __init__(self, ctx, value: Any, tracker: dict):
        caller, base_ctx = _strip_shadow(ctx)
        object.__setattr__(self, "_cordis_value", value)
        object.__setattr__(self, "_cordis_ctx", base_ctx)
        object.__setattr__(self, "_cordis_caller", caller if caller is not None else ctx)
        object.__setattr__(self, "_cordis_tracker", tracker)

    @property
    def __class__(self):
        return type(self._cordis_value)

    def __repr__(self) -> str:
        return repr(self._cordis_value)

    def __getattr__(self, name: str) -> Any:
        tracker = self._cordis_tracker
        if name == tracker.get("property"):
            return self._cordis_ctx
        if name == symbols.caller:
            return self._cordis_caller
        if name == symbols.original:
            return self._cordis_value
        if name.startswith("__cordis_"):
            # internal machinery — JS symbol lookups return the raw member,
            # but Python still needs `self` bound to the view
            inner = getattr(self._cordis_value, name, None)
            if callable(inner):
                func = getattr(inner, "__func__", inner)
                return types.MethodType(func, self)
            return inner

        value = self._cordis_value
        ctx = self._cordis_ctx

        associate = tracker.get("associate")
        if associate is not None:
            prop_name = f"{associate}.{name}"
            if prop_name in ctx.reflect.props:
                result = ctx._reflect_get(prop_name, self)
                # service values that are plain functions need `this` binding
                # (JS member calls bind the traceable proxy); accessor mixins
                # already return bound methods.
                if isinstance(result, types.FunctionType) and not hasattr(result, "__wrapped__"):
                    return types.MethodType(result, self)
                return result

        if not hasattr(value, name):
            return None

        inner = getattr(value, name)

        # Rebind *methods* onto this view, so `self.ctx` inside them is the
        # caller's context. Do not touch anything else.
        #
        # JS reaches this by `value.bind(...)`, where only functions are
        # callable. Python has callable *data* — a dependency-injector
        # Configuration, a functools.partial, a client with __call__ — and
        # `callable(inner)` swept all of it into MethodType(instance, view),
        # which silently turns the attribute into a function that calls the
        # object with the view as its first argument.
        #
        # The discriminator is *where the attribute lives*, not whether it is
        # callable. A method comes from the class. Anything in the instance
        # __dict__ is data the component put there — including a lambda or a
        # bound callback, which `inspect.isfunction` cannot tell from a method.
        if name in getattr(value, "__dict__", ()):
            return inner

        # From the class, then. Only a plain function is a method to rebind;
        # staticmethod/classmethod arrive as their wrapper objects and are
        # correctly left alone, since they take no `self`. Slot descriptors on
        # a __slots__ class are not functions either, so they pass through.
        raw = inspect.getattr_static(type(value), name, None)
        if not inspect.isfunction(raw):
            return inner

        if tracker.get("noShadow"):
            return types.MethodType(raw, self)
        return self._shadow_bind(raw)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_cordis_"):
            object.__setattr__(self, name, value)
            return
        tracker = self._cordis_tracker
        if name == tracker.get("property") or name in (symbols.caller, symbols.original):
            return
        associate = tracker.get("associate")
        if associate is not None:
            prop_name = f"{associate}.{name}"
            if prop_name in self._cordis_ctx.reflect.props:
                self._cordis_ctx._reflect_set(prop_name, value, self)
                return
        setattr(self._cordis_value, name, value)

    def _shadow_self(self) -> ShadowView:
        tracker = self._cordis_tracker
        origin = getattr(self._cordis_value, tracker.get("property"), None)
        if origin is None:
            return self
        shadow_ctx = self._cordis_ctx.extend({"_shadow": origin})
        return ShadowView(self, shadow_ctx)

    def _shadow_bind(self, func) -> Any:
        self_ref = self

        def wrapper(*args, **kwargs):
            result = func(self_ref._shadow_self(), *args, **kwargs)
            # JS `createShadowMethod` re-traces the result with the caller ctx
            return get_traceable(self_ref._cordis_ctx, result)

        functools.update_wrapper(wrapper, func)
        return wrapper

    def __call__(self, *args, **kwargs):
        value = self._cordis_value
        invoke = getattr(value, symbols.invoke, None)
        if invoke is not None:
            receiver = self if self._cordis_tracker.get("noShadow") else self._shadow_self()
            func = getattr(invoke, "__func__", invoke)
            return func(receiver, *args, **kwargs)
        return value(*args, **kwargs)


class _ServiceCallable(Traceable):
    """A callable service extension (JS `createCallable`)."""


class _InstanceView:
    __slots__ = ("_cordis_target", "_cordis_props")

    def __init__(self, target: Any, props: dict):
        object.__setattr__(self, "_cordis_target", target)
        object.__setattr__(self, "_cordis_props", props)

    def __getattr__(self, name: str) -> Any:
        props = object.__getattribute__(self, "_cordis_props")
        if name in props:
            return props[name]
        return getattr(object.__getattribute__(self, "_cordis_target"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_cordis_target", "_cordis_props"):
            object.__setattr__(self, name, value)
            return
        props = object.__getattribute__(self, "_cordis_props")
        if name in props:
            props[name] = value
            return
        setattr(object.__getattribute__(self, "_cordis_target"), name, value)

    def __call__(self, *args, **kwargs):
        return object.__getattribute__(self, "_cordis_target")(*args, **kwargs)

    @property
    def __class__(self):
        return type(object.__getattribute__(self, "_cordis_target"))

    def __repr__(self) -> str:
        return repr(object.__getattribute__(self, "_cordis_target"))
