"""POPO bindings — the component must stay a plain object.

The load-bearing test is `test_component_needs_no_kernel_at_all`: the classes in
this file import nothing from plugkit and are constructed directly. If that
ever stops being true, the design has failed.
"""

import asyncio
from typing import Protocol

import pytest

from plugkit import ConfigService, Context, ReactiveService
from plugkit.binding import provide, snake_case


async def settle(n=15):
    for _ in range(n):
        await asyncio.sleep(0)


# ── the components. Plain classes. No imports, no decorators, no base class. ──


class Database:
    def __init__(self, dsn="sqlite://"):
        self.dsn = dsn
        self.closed = False

    def query(self, sql):
        return f"{self.dsn}:{sql}"

    def close(self):
        self.closed = True


class Cache:
    def __init__(self):
        self.data = {}


class Greeter:
    def __init__(self, database, prefix="hello"):
        self.database = database
        self.prefix = prefix

    def hello(self, name):
        return f"{self.prefix} {name}"


class Timeouts:
    """Holds reactive state. Still imports nothing: `reactive` is a parameter."""

    def __init__(self, reactive, seconds=30):
        self.seen = []
        self.seconds = reactive.signal(seconds)
        self._reactive = reactive
        reactive.effect(lambda: self.seen.append(self.seconds.get()))

    def follow(self, sink):
        self._reactive.effect(lambda: sink.append(self.seconds.get()))


class GreeterDeps(Protocol):
    database: Database
    cache: Cache


# ── tests ────────────────────────────────────────────────────────────────────


def test_component_needs_no_kernel_at_all():
    """Constructible and testable with no kernel in sight."""
    greeter = Greeter(database=Database(dsn="fake://"), prefix="hi")
    assert greeter.hello("world") == "hi world"
    assert greeter.database.query("SELECT 1") == "fake://:SELECT 1"


def test_snake_case_default_names():
    assert snake_case("Greeter") == "greeter"
    assert snake_case("HTTPClient") == "http_client"
    assert snake_case("MCPServerManager") == "mcp_server_manager"


async def test_provide_registers_and_injects():
    root = Context()
    await root.plugin(provide(Database, "database"))
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    assert root.greeter.hello("world") == "hello world"
    assert root.greeter.database.query("SELECT 1") == "sqlite://:SELECT 1"


async def test_needs_dict_renames_the_kwarg():
    class Wrapper:
        def __init__(self, db):
            self.db = db

    root = Context()
    await root.plugin(provide(Database, "database"))
    await root.plugin(provide(Wrapper, "wrapper", needs={"db": "database"}))
    await settle()
    assert root.wrapper.db is not None


async def test_needs_protocol_derives_the_inject_list():
    """One Protocol drives both the runtime wiring and the type checker."""
    plugin = provide(Greeter, "greeter", needs=GreeterDeps)
    assert plugin["inject"] == ["cache", "database"]


async def test_binding_waits_for_its_dependency():
    root = Context()
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()
    assert getattr(root, "greeter", None) is None, "activated without its dependency"

    await root.plugin(provide(Database, "database"))
    await settle()
    assert root.greeter.hello("x") == "hello x"


async def test_config_reaches_the_constructor():
    root = Context()
    await root.plugin(ConfigService, {"dict": {"db": {"dsn": "pg://prod"}}})
    await root.plugin(provide(Database, "database", config={"dsn": ("db.dsn", "sqlite://")}))
    await settle()
    assert root.database.dsn == "pg://prod"


async def test_config_default_when_key_is_absent():
    root = Context()
    await root.plugin(ConfigService)
    await root.plugin(provide(Database, "database", config={"dsn": ("db.dsn", "sqlite://")}))
    await settle()
    assert root.database.dsn == "sqlite://"


async def test_close_runs_on_unload():
    root = Context()
    fiber = await root.plugin(provide(Database, "database"))
    await settle()
    component = root.database
    assert component.closed is False

    await fiber.dispose()
    await settle()
    assert component.closed is True, "close() was not called on unload"


async def test_close_false_skips_teardown():
    root = Context()
    fiber = await root.plugin(provide(Database, "database", close=False))
    await settle()
    component = root.database
    await fiber.dispose()
    await settle()
    assert component.closed is False


async def test_named_close_method():
    class Server:
        def __init__(self):
            self.stopped = False

        def shutdown(self):
            self.stopped = True

    root = Context()
    fiber = await root.plugin(provide(Server, "server", close="shutdown"))
    await settle()
    server = root.server
    await fiber.dispose()
    await settle()
    assert server.stopped is True


async def test_bad_close_name_is_loud():
    class Thing:
        pass

    root = Context()
    with pytest.raises(AttributeError, match="nope"):
        await root.plugin(provide(Thing, "thing", close="nope"))


async def test_config_change_rebuilds_the_component():
    """A constructor argument cannot be mutated, so the binding restarts."""
    root = Context()
    await root.plugin(ReactiveService)
    await root.plugin(ConfigService, {"dict": {"db": {"dsn": "one://"}}})
    await root.plugin(provide(Database, "database", config={"dsn": ("db.dsn", "sqlite://")}))
    await settle()

    first = root.database
    assert first.dsn == "one://"

    root.config.set("db.dsn", "two://")
    await settle(40)

    assert root.database.dsn == "two://"
    assert root.database is not first, "the component was not rebuilt"
    assert first.closed is True, "the replaced component was not closed"


async def test_binding_works_without_reactive_mounted():
    """Reacting to config is opt-in; a minimal composition must still boot."""
    root = Context()
    await root.plugin(ConfigService, {"dict": {"db": {"dsn": "one://"}}})
    await root.plugin(provide(Database, "database", config={"dsn": ("db.dsn", "sqlite://")}))
    await settle()
    assert root.database.dsn == "one://"

    root.config.set("db.dsn", "two://")
    await settle()
    assert root.database.dsn == "one://", "rebuilt without ReactiveService mounted"


async def test_a_plain_component_registers_fiber_owned_effects():
    """Reactive state reaches a plain class without marking the class up.

    `reactive` arrives as an ordinary constructor argument, and what the class
    stores is a view already addressed to the binding's fiber — so an effect it
    registers later, from a method, is owned by that fiber too. Unload stops
    both, and registering afterwards raises instead of leaking.
    """
    root = Context()
    await root.plugin(ReactiveService)
    fiber = await root.plugin(provide(Timeouts, "timeouts", needs=["reactive"]))
    await settle()

    timeouts = root.timeouts
    assert timeouts.seen == [30], "the effect made in __init__ did not run"

    late = []
    timeouts.follow(late)
    timeouts.seconds.set(45)
    await settle()
    assert timeouts.seen == [30, 45]
    assert late == [30, 45], "an effect registered from a method did not follow"

    await fiber.dispose()
    await settle()
    timeouts.seconds.set(99)
    await settle()
    assert timeouts.seen == [30, 45], "an effect outlived the fiber that owned it"
    assert late == [30, 45], "the method's effect outlived the fiber"

    with pytest.raises(Exception, match="inactive"):
        timeouts.follow([])


def test_a_component_holding_signals_still_needs_no_kernel():
    """The stand-in for `reactive` in a unit test is two lines of stdlib."""
    import types

    from plugkit import Effect, Signal

    seen = []
    timeouts = Timeouts(types.SimpleNamespace(signal=Signal, effect=Effect), seconds=5)
    timeouts.follow(seen)
    timeouts.seconds.set(7)

    assert timeouts.seen == [5, 7]
    assert seen == [5, 7]


async def test_a_context_manager_component_is_entered():
    """Teardown must go through the protocol setup went through.

    `__exit__` on an object that was never entered is a lock released without
    being acquired, a transaction rolled back that never began. The binding used
    to do exactly that, and the component could not tell.
    """
    log = []

    class Pool:
        def __enter__(self):
            log.append("enter")
            return self

        def __exit__(self, *exc):
            log.append("exit")

    root = Context()
    fiber = await root.plugin(provide(Pool, "pool"))
    await settle()
    assert log == ["enter"]

    await fiber.dispose()
    await settle()
    assert log == ["enter", "exit"]


async def test_the_service_is_what_enter_returned():
    """The context-manager protocol says `__enter__`'s value is the resource."""

    class Handle:
        pass

    handle = Handle()

    class Opener:
        def __enter__(self):
            return handle

        def __exit__(self, *exc):
            pass

    root = Context()
    await root.plugin(provide(Opener, "resource"))
    await settle()
    assert root.resource is handle


async def test_close_beats_the_context_manager_protocol():
    """A component with both keeps using `close()`, and is not entered."""
    log = []

    class Both:
        def close(self):
            log.append("close")

        def __enter__(self):
            log.append("enter")
            return self

        def __exit__(self, *exc):
            log.append("exit")

    root = Context()
    fiber = await root.plugin(provide(Both, "both"))
    await settle()
    await fiber.dispose()
    await settle()
    assert log == ["close"]


async def test_an_async_only_context_manager_says_what_to_do():
    """Entering needs an await, and apply is synchronous. Say so, don't guess."""

    class AsyncPool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    root = Context()
    fiber = root.plugin(provide(AsyncPool, "pool"))
    with pytest.raises(TypeError, match="close="):
        await fiber


# ── construction that needs an await: the init hook ───────────────────────────


class Pool:
    """A plain class whose readiness needs an await. Imports nothing."""

    def __init__(self, dsn="sqlite://"):
        self.dsn = dsn
        self.connected = False
        self.closed = False

    async def connect(self):
        await asyncio.sleep(0)
        self.connected = True

    def close(self):
        self.closed = True


async def test_the_init_hook_runs_after_the_service_is_registered():
    """Cordis registers from the constructor and awaits the init hook after.

    `service.ts` registers the instance synchronously; `fiber.ts` then runs the
    init hook and awaits it. `provide(init=...)` is that order for a plain class.
    """
    root = Context()
    fiber = await root.plugin(provide(Pool, "pool", init="connect"))
    await settle()

    assert root.pool.connected is True, "the init hook did not run"

    pool = root.pool
    await fiber.dispose()
    await settle()
    assert pool.closed is True, "init= displaced the ordinary teardown"


async def test_a_dependent_does_not_see_a_half_built_service():
    """The property `test_pending_inject` holds upstream: injection waits for init.

    The service is registered before the hook runs, so the guarantee cannot come
    from registration order — it comes from the fiber still loading, which is
    what makes the name unresolvable to anyone injecting it.
    """
    release = asyncio.Event()
    seen = []

    class Slow:
        def __init__(self):
            self.ready = False

        async def start(self):
            await release.wait()
            self.ready = True

    root = Context()
    # Not awaited: awaiting this mount would wait for the init hook itself, and
    # the point of the test is what everyone *else* sees while it runs.
    root.plugin(provide(Slow, "slow", init="start"))

    def dependent(ctx, config=None):
        seen.append(ctx.slow.ready)

    dependent.inject = ["slow"]
    await root.plugin(dependent)
    await settle()
    assert seen == [], "a dependent ran while the service was still initialising"

    release.set()
    await settle()
    assert seen == [True], "the dependent did not run once init finished"


async def test_a_sync_init_hook_is_allowed():
    class Cursor:
        def __init__(self):
            self.opened = False

        def open(self):
            self.opened = True

    root = Context()
    await root.plugin(provide(Cursor, "cursor", init="open"))
    await settle()
    assert root.cursor.opened is True


async def test_a_failing_init_hook_fails_the_fiber():
    class Broken:
        async def start(self):
            raise RuntimeError("no connection")

    root = Context()
    fiber = root.plugin(provide(Broken, "broken", init="start"))
    with pytest.raises(RuntimeError, match="no connection"):
        await fiber


async def test_an_async_factory_is_refused_and_says_what_to_do():
    """Registering the coroutine was the silent failure; refusing is the loud one."""

    async def connect(dsn="sqlite://"):
        return Database(dsn)

    root = Context()
    fiber = root.plugin(provide(connect, "db"))
    with pytest.raises(TypeError, match="init="):
        await fiber


async def test_a_bad_init_name_is_loud():
    root = Context()
    fiber = root.plugin(provide(Database, "database", init="nope"))
    with pytest.raises(AttributeError, match="nope"):
        await fiber


async def test_mount_config_cannot_shadow_an_injected_service():
    """A mount config naming an injected service is a composition mistake.

    It used to win silently: the constructor received the literal and the
    service object was dropped, while the fiber went on declaring the dependency
    and reloading when the real service was replaced. The caller cannot mean both
    things, so the binding says so instead of picking one.
    """
    root = Context()
    await root.plugin(provide(Database, "database"))
    fiber = root.plugin(
        provide(Greeter, "greeter", needs=["database"]), {"database": "not-a-database"}
    )
    with pytest.raises(TypeError, match="database"):
        await fiber


async def test_mount_config_still_overrides_a_config_argument():
    """Only `needs` is protected. Overriding a config-derived kwarg is the point."""
    root = Context()
    await root.plugin(ConfigService, {"dict": {"greeter": {"prefix": "from-config"}}})
    await root.plugin(provide(Database, "database"))
    await root.plugin(
        provide(
            Greeter,
            "greeter",
            needs=["database"],
            config={"prefix": ("greeter.prefix", "hello")},
        ),
        {"prefix": "from-mount"},
    )
    await settle()
    assert root.greeter.prefix == "from-mount"


async def test_bad_needs_type_is_loud():
    with pytest.raises(TypeError, match="must be a list"):
        provide(Greeter, "greeter", needs=42)


# ── the @plugin decorator ────────────────────────────────────────────────


async def test_plugin_decorator_carries_inject():
    from plugkit.binding import plugin

    @plugin(inject=["database"])
    def uses_db(ctx, config=None):
        ctx.database.query("SELECT 1")

    assert uses_db["inject"] == ["database"]
    assert uses_db["name"] == "uses_db"

    root = Context()
    await root.plugin(provide(Database, "database"))
    await root.plugin(uses_db)
    await settle()


async def test_plugin_decorator_derives_inject_from_the_annotation():
    from plugkit.binding import plugin

    class Deps(Protocol):
        database: Database
        def on(self, event: str, listener: object) -> object: ...

    @plugin
    def reader(ctx: Deps, config=None):
        pass

    assert reader["inject"] == ["database"], "context methods leaked into inject"


def test_context_members_are_current():
    """CONTEXT_MEMBERS mirrors what cordis mixes onto a context. Catch drift."""
    from plugkit import Context
    from plugkit.binding import CONTEXT_MEMBERS

    root = Context()
    # timer members only exist when the timer plugin is mounted
    timer = {"timeout", "interval", "throttle", "debounce", "setTimeout", "setInterval"}
    missing = [name for name in CONTEXT_MEMBERS - timer if not hasattr(root, name)]
    assert not missing, f"listed as context members but absent from a real Context: {missing}"


# ── construction policy is a plugin, not the kernel ──────────────────────


async def test_a_rival_construction_policy_needs_no_kernel_change():
    """provide() registers one instance; provide_factory() registers a maker.

    Both are ordinary plugins touching the kernel through the same three
    methods. This is the answer to "is DI itself a plugin?" — the resolution
    and lifetime machinery is not, everything above it is.
    """
    from plugkit.examples.alternative_binding import provide_factory

    root = Context()
    await root.plugin(provide(Database, "database"))
    await root.plugin(provide_factory(Greeter, "greeter", needs=["database"]))
    await settle()

    first = root.greeter()
    second = root.greeter()
    assert first is not second, "a factory policy handed out a shared instance"
    assert first.hello("x") == "hello x"


async def test_a_factory_policy_still_unwinds_with_its_fiber():
    from plugkit.examples.alternative_binding import provide_factory

    root = Context()
    fiber = await root.plugin(provide_factory(Database, "db"))
    await settle()
    made = [root.db(), root.db()]
    assert not any(d.closed for d in made)

    await fiber.dispose()
    await settle()
    assert all(d.closed for d in made), "instances outlived the plugin that made them"


async def test_forgetting_the_service_name_is_a_hard_error():
    """No default name: it read as type-based injection and broke silently.

    Python raises before the body runs, which is the point — pyright catches it
    at author time rather than leaving a plugin that never activates.
    """
    with pytest.raises(TypeError, match="required positional argument"):
        provide(Database)


async def test_a_bad_service_name_says_what_to_pass():
    """The body's own check covers what Python's cannot: wrong type, or empty."""
    with pytest.raises(TypeError, match='provide\\(Database, "database"\\)'):
        provide(Database, "")
    with pytest.raises(TypeError, match="needs a service name"):
        provide(Database, 42)


async def test_the_service_name_is_the_link_not_the_class():
    """Renaming the class must not change the wiring."""

    class PostgresDatabase(Database):
        pass

    root = Context()
    await root.plugin(provide(PostgresDatabase, "database"))
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()
    assert root.greeter.hello("x") == "hello x"


def test_the_public_surface_is_importable():
    """Everything the docs tell a reader to import must import.

    `plugin` was documented and not exported; `from plugkit import plugin`
    raised ImportError.
    """
    import plugkit

    for name in plugkit.__all__:
        assert hasattr(plugkit, name), f"{name} is in __all__ but not importable"

    from plugkit import Context, plugin, provide, bind, snake_case  # noqa: F401


def test_every_export_is_exercised_somewhere():
    """An export with no test is either public surface that needs one, or not
    public and should not be exported. This keeps that decision honest."""
    import pathlib
    import re

    import plugkit

    tests = " ".join(
        p.read_text() for p in pathlib.Path(__file__).parent.rglob("*.py")
    )
    untested = [n for n in plugkit.__all__ if not re.search(rf"\b{re.escape(n)}\b", tests)]
    assert not untested, f"exported but never used in a test: {untested}"
