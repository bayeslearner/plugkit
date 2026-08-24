"""Internal utilities for cordis — the pure Python port of `cordis`."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Symbols
#
# JavaScript Cordis uses unique symbols as computed member keys.  Python
# attributes must be strings, so every symbol is a `str` subclass whose value
# is the internal attribute name it maps to.  `getattr(obj, Service.init)`
# therefore works exactly like `obj[Service.init]` in JavaScript.
# ---------------------------------------------------------------------------


class _Symbol(str):
    """A string constant with symbol-like identity, mirroring a JS unique
    symbol used as a *member key*.  `getattr(obj, Service.init)` must work, so
    str equality and hashing are kept."""

    def __repr__(self) -> str:
        return f"Symbol({str.__repr__(self)})"


def symbol(name: str) -> "_Symbol":
    return _Symbol(name)


class UniqueSymbol:
    """The Python equivalent of JS `Symbol(name)` — a unique, non-string key
    used for isolation realms.  Two calls never compare equal."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"Symbol({self.name!r})"

    __eq__ = object.__eq__
    __hash__ = object.__hash__


def unique_symbol(name: str) -> UniqueSymbol:
    return UniqueSymbol(name)


class ChainedDict(dict):
    """A dict with a live parent chain — the Python equivalent of the JS
    `Object.create(map)` isolate/intercept maps.  `get()` walks the chain, so
    keys added to the parent later remain visible."""

    def __init__(self, *args, parent=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._parent = parent

    def get(self, key, default=None):
        obj = self
        while obj is not None:
            if key in obj:
                return obj[key]
            obj = obj._parent
        return default


class symbols:
    """Namespace of internal cordis symbols."""

    # internal symbols
    shadow = symbol("_shadow")
    caller = symbol("__cordis_caller__")
    receiver = symbol("__cordis_receiver__")
    original = symbol("__cordis_original__")
    metadata = symbol("__cordis_metadata__")
    init_hooks = symbol("__cordis_init_hooks__")
    check_proto = symbol("__cordis_check_proto__")

    # context symbols
    effect = symbol("__cordis_effect__")
    filter = symbol("_filter")
    isolate = symbol("_isolate")
    intercept = symbol("_intercept")

    # service symbols
    init = symbol("__cordis_init__")
    check = symbol("__cordis_check__")
    config = symbol("__cordis_config__")
    invoke = symbol("__cordis_invoke__")
    extend = symbol("__cordis_extend__")
    tracker = symbol("__cordis_tracker__")
    resolveConfig = symbol("__cordis_resolve_config__")


# ---------------------------------------------------------------------------
# DisposableList
# ---------------------------------------------------------------------------


class DisposableList:
    """An insertion-ordered collection with `push`/`delete`/`clear`."""

    def __init__(self) -> None:
        self._sn = 0
        self._map: dict[int, Any] = {}
        self._reverse: dict[int, int] = {}

    @property
    def length(self) -> int:
        return len(self._map)

    def __len__(self) -> int:
        return len(self._map)

    def push(self, value: Any) -> Callable[[], None]:
        self._sn += 1
        sn = self._sn
        self._map[sn] = value
        self._reverse[id(value)] = sn

        def remover() -> None:
            self._map.pop(sn, None)

        return remover

    def delete(self, value: Any) -> bool:
        sn = self._reverse.pop(id(value), None)
        if sn is None:
            return False
        self._map.pop(sn, None)
        return True

    def clear(self) -> list[Any]:
        values = list(self._map.values())
        self._map.clear()
        self._reverse.clear()
        values.reverse()
        return values

    def __iter__(self):
        return iter(self._map.values())


# ---------------------------------------------------------------------------
# Tracker & traceability
# ---------------------------------------------------------------------------

Tracker = dict  # { associate?, property?, noShadow? }


def is_object(value: Any) -> bool:
    """Mirror JS `value && (typeof value === 'object' || typeof value === 'function')`."""
    if value is None:
        return False
    if callable(value):
        return True
    return not isinstance(value, (bool, int, float, complex, str, bytes, bytearray))


def is_nullish(value: Any) -> bool:
    return value is None


def get_property_chain(obj: Any, name: str):
    """Walk obj's class hierarchy to find the first definition of `name`."""
    for cls in inspect.getmro(type(obj)):
        if name in cls.__dict__:
            return cls.__dict__[name]
    return None


# ---------------------------------------------------------------------------
# Error composition
# ---------------------------------------------------------------------------


def build_outer_stack(offset: int = 0) -> Callable[[], list[str]]:
    """Capture the current stack frame list for later error decoration."""
    import traceback

    stack = traceback.extract_stack()[:-1 - offset]
    return lambda: [f"    at {frame.filename}:{frame.lineno} in {frame.name}" for frame in stack]


def compose_error(callback, get_outer_stack=None):
    """Run `callback`; on exception, decorate it with the outer stack."""
    try:
        return callback()
    except BaseException as error:
        if get_outer_stack is not None:
            lines = get_outer_stack()
            if lines:
                note = "\n".join(lines)
                try:
                    error.add_note(f"\nouter stack:\n{note}")
                except AttributeError:  # pragma: no cover
                    pass
        raise


def hyphenate(source: str) -> str:
    """`paramCase`-style conversion: camelCase / PascalCase to kebab-case."""
    import re

    source = re.sub(r"([a-z\d])([A-Z])", r"\1-\2", source)
    source = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", source)
    source = re.sub(r"[\s_]+", "-", source)
    return source.lower()


def is_constructor(func: Any) -> bool:
    return inspect.isclass(func)


def is_iterable(value: Any) -> bool:
    return hasattr(value, "__iter__")


def is_async_iterable(value: Any) -> bool:
    return hasattr(value, "__aiter__")


class List:
    """Port of `@cordisjs/utils` List — a fiber-scoped push collection."""

    def __init__(self, ctx, trace: str):
        self.__cordis_tracker__ = {"property": "ctx"}
        self.ctx = ctx
        self._trace = trace
        self._sn = 0
        self._inner: dict = {}

    @property
    def length(self) -> int:
        return len(self._inner)

    def __len__(self) -> int:
        return len(self._inner)

    def push(self, value):
        self_ = self

        def effect():
            self_._sn += 1
            sn = self_._sn
            self_._inner[sn] = value

            def dispose():
                del self_._inner[sn]

            return dispose

        self.ctx.effect(effect, f"{self._trace}.push()")

    def filter(self, predicate):
        for value in self._inner.values():
            if predicate(value):
                yield value

    def map(self, mapper):
        for value in self._inner.values():
            yield mapper(value)

    def __iter__(self):
        return iter(self._inner.values())


# ── dispatch carrier ────────────────────────────────────────────────────
# Cordis binds the dispatch carrier as a listener's `this`. Python has no
# `this`, and passing it as a leading positional argument would change every
# listener's arity away from what dsh's plugins and docs assume. So it is
# ambient instead: listeners take exactly the event arguments, and the few
# that need the carrier read it with `this_()`.
#
# Valid only during the synchronous body of a listener. An async listener
# keeps it across its own awaits (the ContextVar is set inside the awaiting
# task), but a coroutine returned from a sync dispatch mode and awaited by
# someone else does not.

import inspect as _inspect
from contextvars import ContextVar as _ContextVar

_carrier: _ContextVar = _ContextVar("cordis_carrier", default=None)


def this_():
    """The carrier of the dispatch currently running, or None."""
    return _carrier.get()


def call_listener(callback, carrier, args):
    """Invoke a listener with `carrier` ambient rather than positional."""
    token = _carrier.set(carrier)
    try:
        return callback(*args)
    finally:
        _carrier.reset(token)


async def acall_listener(callback, carrier, args):
    """Async form: the carrier stays set across the listener's own awaits."""
    token = _carrier.set(carrier)
    try:
        result = callback(*args)
        if _inspect.isawaitable(result):
            result = await result
        return result
    finally:
        _carrier.reset(token)

