"""Bindings — how a plain class becomes a service without importing the kernel.

The complaint this answers: in 1.0 a component was a class covered in
`@component`, `@provides`, `@requires` decorators, so it imported the kernel and
stopped being a plain object. Cordis is no better on this axis — `class Foo(Service)`
is a base class, and `def apply(ctx, config)` takes a kernel-shaped argument.

So the component and its registration are split. The component is a plain class
that knows nothing about any of this:

    # services/greeter.py — imports nothing from plugkit
    class Greeter:
        def __init__(self, db, prefix="hello"):
            self.db = db
            self.prefix = prefix
        def hello(self, name):
            return f"{self.prefix} {name}"
        def close(self):
            self.db = None

and one line elsewhere says how to wire it:

    # app.py — the only kernel-aware file
    from plugkit import provide
    from services.greeter import Greeter

    greeter = provide(Greeter, "greeter", needs=["db"], config={"prefix": "greeter.prefix"})
    await root.plugin(greeter)

`Greeter` stays constructible in a test as `Greeter(db=FakeDB(), prefix="hi")`,
with no kernel, no container, no fixtures.

## Declaring what you need, once

`needs` takes a list, a dict, or — best — a Protocol:

    class GreeterDeps(Protocol):
        db: Database
        cache: Cache

    provide(Greeter, "greeter", needs=GreeterDeps)

`typing.get_protocol_members` (Python 3.13+) reads the member names off the
Protocol, so the same declaration drives the runtime injection *and* the type
checker. Annotate the constructor with the same types and pyright checks the
component against exactly what the kernel will hand it. One source of truth.

## Config

`config={"prefix": "greeter.prefix"}` reads `ctx.config.get("greeter.prefix")`.
Use a `(key, default)` tuple to supply a default. If `ReactiveService` is
mounted, a change to any of those keys restarts the binding's fiber, which
disposes the old object and constructs a new one from the new values — the
Cordis-correct outcome, since a constructor argument cannot be changed in place.

## Teardown

On unload the binding calls `close()` or `aclose()` if the object has one, or
`__exit__`/`__aexit__` if it is a context manager. Name a different method with
`close="shutdown"`, or pass `close=False` to disable.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Iterable, Mapping

__all__ = ["provide", "plugin", "bind", "snake_case", "CONTEXT_MEMBERS"]


def plugin(fn: Callable | None = None, *, inject: Any = None, name: str | None = None):
    """Mark a function as a plugin, and say what it needs.

    Cordis's own idiom is `fn.inject = [...]`, which works at runtime and is
    rejected by pyright — you cannot assign arbitrary attributes to a function.
    So this returns a plugin mapping instead, which typechecks and which the
    registry already understands.

    With no `inject`, the list is read off the annotation of the first
    parameter, if that annotation is a Protocol:

        class ReportDeps(Protocol):
            database: Database
            def on(self, event: str, listener: Any) -> Any: ...

        @plugin
        def report(ctx: ReportDeps, config=None) -> None:
            ctx.database.query("SELECT 1")     # typed
        # inject == ["database"]

    Members that already exist on `Context` (`on`, `effect`, `emit`, ...) are
    dropped, so a Protocol may describe the context methods it calls without
    those turning into phantom service dependencies.
    """

    def decorate(target: Callable):
        needs = inject
        if needs is None:
            needs = _inject_from_annotation(target)
        return {
            "name": name or getattr(target, "__name__", "plugin"),
            "inject": sorted(_resolve_needs(needs).values()),
            "apply": target,
        }

    return decorate if fn is None else decorate(fn)


# Names that are context *methods*, not services. A Protocol may describe the
# context calls a plugin makes — `on`, `effect`, `emit` — and those must not
# become phantom service dependencies.
#
# `dir(Context)` does not answer this: these are mixed onto the context at
# runtime, so the class object does not carry them. The list mirrors the
# `self.mixin(...)` calls in `cordis/reflect.py` (and `cordis/timer.py`), and
# `test_context_members_are_current` fails if the two drift apart.
CONTEXT_MEMBERS = frozenset(
    {
        # reflect
        "get", "set", "provide", "accessor", "mixin",
        # fiber
        "runtime", "effect",
        # registry
        "inject", "plugin",
        # events
        "on", "once", "parallel", "emit", "serial", "bail", "waterfall",
        # timer, when mounted
        "timeout", "interval", "throttle", "debounce", "setTimeout", "setInterval",
        # context's own
        "extend", "isolate", "intercept", "root", "fiber", "reflect",
        "registry", "events", "logger", "baseUrl",
    }
)


def _inject_from_annotation(target: Callable) -> list[str]:
    """Service names from the Protocol annotating the first parameter."""
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return []
    parameters = list(signature.parameters.values())
    if not parameters:
        return []
    annotation = parameters[0].annotation
    if annotation is inspect.Parameter.empty or not inspect.isclass(annotation):
        return []
    try:
        from typing import get_protocol_members

        members = set(get_protocol_members(annotation))
    except (ImportError, TypeError):
        return []

    return sorted(members - CONTEXT_MEMBERS)

_CAMEL_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(name: str) -> str:
    """`HTTPClient` -> `http_client`.

    A helper for picking a service name, not a default — `provide()` makes you
    name the service yourself. See the note in its docstring for why.
    """
    return _CAMEL_2.sub(r"\1_\2", _CAMEL_1.sub(r"\1_\2", name)).lower()


def _resolve_needs(needs: Any) -> dict[str, str]:
    """Normalise `needs` to {constructor_kwarg: service_name}."""
    if needs is None:
        return {}
    if isinstance(needs, Mapping):
        return dict(needs)
    if isinstance(needs, str):
        return {needs: needs}
    # a Protocol: its members are both the kwargs and the service names
    if inspect.isclass(needs):
        try:
            from typing import get_protocol_members

            return {member: member for member in sorted(get_protocol_members(needs))}
        except (ImportError, TypeError) as exc:
            raise TypeError(
                f"needs={needs!r} is a class but not a Protocol "
                "(needs a list, a dict, or a typing.Protocol)"
            ) from exc
    if isinstance(needs, Iterable):
        return {name: name for name in needs}
    raise TypeError(f"needs={needs!r} must be a list, dict, Protocol, or str")


def _resolve_config(config: Any) -> dict[str, tuple[str, Any]]:
    """Normalise `config` to {constructor_kwarg: (dotted_key, default)}."""
    if not config:
        return {}
    out: dict[str, tuple[str, Any]] = {}
    for kwarg, spec in config.items():
        if isinstance(spec, tuple):
            key, default = spec
        else:
            key, default = spec, None
        out[kwarg] = (key, default)
    return out


def _setup_teardown(obj: Any, close: Any) -> tuple[Any, Callable[[], Any] | None]:
    """`(component_to_register, teardown_or_None)`.

    A context manager is **entered here**, and what `__enter__` returned is what
    gets registered — that is what the protocol says the resource is. Calling
    `__exit__` on an object that was never entered is a lock released without
    being acquired and a transaction rolled back that never began; the binding
    used to do that, and the component had no way to tell.

    An explicit `close=` and a `close`/`aclose`/`shutdown`/`dispose` method both
    win over the protocol, so a component carrying both is not entered.
    """
    if close is False:
        return obj, None
    if isinstance(close, str):
        method = getattr(obj, close, None)
        if method is None:
            raise AttributeError(f"{type(obj).__name__} has no method {close!r} to close with")
        return obj, method
    for name in ("close", "aclose", "shutdown", "dispose"):
        method = getattr(obj, name, None)
        if callable(method):
            return obj, method
    if hasattr(obj, "__enter__") and hasattr(obj, "__exit__"):
        entered = obj.__enter__()
        if entered is None:
            raise TypeError(
                f"{type(obj).__name__}.__enter__() returned None, so there is no "
                f"component to register. Return the resource, or name the teardown "
                f"with close=."
            )
        return entered, lambda: obj.__exit__(None, None, None)
    if hasattr(obj, "__aenter__"):
        raise TypeError(
            f"{type(obj).__name__} is an async context manager, and entering it "
            f"needs an await that construction does not have. Name the teardown "
            f"with close=\"aclose\" (or whichever method), or close=False."
        )
    return obj, None


def provide(
    factory: Callable[..., Any],
    service_name: str,
    *,
    needs: Any = None,
    config: Mapping[str, Any] | None = None,
    close: Any = None,
    name: str | None = None,
    extra: Mapping[str, Any] | None = None,
):
    """Wrap a plain class or factory as a plugin that registers one service.

        provide(PostgresDatabase, "database")
        provide(Greeter, "greeter", needs=["database"])

    **The service name is required, deliberately.** Everything in this kernel is
    looked up by string name, so the name *is* the component's public interface.
    An earlier version defaulted it to `snake_case(factory.__name__)`, which made
    two things go wrong at once:

    - It read like type-based injection. It is not — `needs=["database"]` means
      "the service named `database`", and the class is never consulted. It only
      appeared to work because `Database` snake_cases to `database`.
    - Renaming the class silently broke the wiring. `Database` → `PostgresDatabase`
      changes the service name to `postgres_database`, so every dependent stops
      activating, with no error, because a plugin waiting on a service that never
      arrives is indistinguishable from one that is not needed yet.

    Naming it yourself also pushes you toward the *role* rather than the
    implementation, which is what makes swapping implementations possible:
    `provide(PostgresDatabase, "database")` today, `provide(SqliteDatabase,
    "database")` tomorrow, and no dependent changes.

    Args:
        factory: the component — a plain class or any callable. Never sees `ctx`.
        service_name: the name to register under. Other plugins ask for this string.
        needs: services to pass to the constructor. List, dict, Protocol, or str.
            A list means "the kwarg and the service share a name"; use a dict
            (`{"db": "database"}`) when they differ.
        config: constructor kwargs read from `ctx.config`, as `{kwarg: key}` or
            `{kwarg: (key, default)}`.
        close: teardown method name, or False to skip. Auto-detected by default.
        name: plugin name shown in fiber diagnostics. Defaults to `service_name`.
        extra: literal constructor kwargs, passed through untouched.

    Returns:
        A plugin mapping suitable for `ctx.plugin(...)`.
    """
    if not isinstance(service_name, str) or not service_name:
        raise TypeError(
            f"provide() needs a service name as its second argument, got {service_name!r}. "
            f'Try provide({getattr(factory, "__name__", "Thing")}, '
            f'"{snake_case(getattr(factory, "__name__", "thing"))}").'
        )
    wiring = _resolve_needs(needs)
    config_spec = _resolve_config(config)
    literals = dict(extra or {})

    required = set(wiring.values())
    if config_spec:
        required.add("config")
    inject = sorted(required)

    def apply(ctx, plugin_config=None):
        kwargs = dict(literals)
        for kwarg, service in wiring.items():
            kwargs[kwarg] = getattr(ctx, service)
        for kwarg, (key, default) in config_spec.items():
            kwargs[kwarg] = ctx.config.get(key, default)
        if isinstance(plugin_config, Mapping):
            # A mount config may override a literal or a config-derived argument
            # — that is what per-mount settings are for. It may not override an
            # injected service: the fiber goes on declaring that dependency and
            # reloading when its provider is replaced, so a literal in its place
            # is a component holding something the kernel thinks it does not
            # have. The caller cannot mean both, so neither is chosen silently.
            shadowed = sorted(set(plugin_config) & set(wiring))
            if shadowed:
                raise TypeError(
                    f"mount config for {name or service_name!r} sets "
                    f"{', '.join(map(repr, shadowed))}, which "
                    f"{'is' if len(shadowed) == 1 else 'are'} injected via needs. "
                    f"Drop the key, or drop it from needs and pass the value in extra."
                )
            kwargs.update(plugin_config)

        component, closer = _setup_teardown(factory(**kwargs), close)
        ctx.provide(service_name, component)

        _watch_config(ctx, config_spec)

        if closer is None:
            return None

        def dispose():
            return closer()

        return dispose

    plugin = {"name": name or service_name, "inject": inject, "apply": apply}
    # carried for tests and diagnostics; the kernel only reads name/inject/apply
    plugin["factory"] = factory
    plugin["provides"] = service_name
    return plugin


def _watch_config(ctx, config_spec: dict[str, tuple[str, Any]]) -> None:
    """Restart this binding when one of its config keys changes.

    A constructor argument cannot be changed after construction, so the honest
    response to a config change is to build a new component — which is exactly
    what restarting the fiber does: the old disposers run, `apply` runs again.

    `ReactiveService` is an *optional* dependency. It cannot go in the binding's
    own `inject`, because that would stop the binding activating at all in a
    composition without it. `ctx.inject(...)` is the primitive for this: it
    mounts a child plugin that runs only while the named services exist, and
    unloads when they go away.

    Note the deliberate `ctx.reactive` inside the child rather than the parent —
    Cordis refuses to read a service the current fiber did not inject, which is
    what lets the epoch be trusted. A `getattr(ctx, "reactive", None)` here
    would swallow that as None and silently never watch anything.
    """
    if not config_spec:
        return

    owner = ctx.fiber

    def watcher(inner_ctx, _config=None):
        seen: dict[str, Any] = {}

        def watch():
            current = {
                key: inner_ctx.config.get(key, default)
                for key, default in config_spec.values()
            }
            if not seen:
                seen.update(current)
                return
            if current != seen:
                seen.clear()
                seen.update(current)
                owner.restart()

        inner_ctx.reactive.effect(watch)

    ctx.inject(["reactive", "config"], watcher)


def bind(**bindings) -> list:
    """Several `provide()` results at once, for a composition root.

        plugins = bind(
            db=provide(Database, "db", config={"dsn": "db.dsn"}),
            greeter=provide(Greeter, "greeter", needs=["db"]),
        )
        for plugin in plugins:
            await root.plugin(plugin)

    Order does not matter — `inject` decides activation, not position.
    """
    return list(bindings.values())
