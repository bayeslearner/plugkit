"""Run the README examples and assert the claims made around them.

The README once showed `return ctx.server.add_route(...)` with the comment
"returns its undo". No such API exists. A plain class's method returns whatever
its author wrote, and the docs described a capability the code did not have.

Change an example, change it here.
"""

import asyncio

from plugkit import Context, provide


async def settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0)


# ── the quickstart ────────────────────────────────────────────────────────


class Database:
    def __init__(self, dsn="sqlite://"):
        self.dsn = dsn
        self.closed = False

    def close(self):
        self.closed = True


class Greeter:
    def __init__(self, database, prefix="hello"):
        self.database = database
        self.prefix = prefix

    def hello(self, name):
        return f"{self.prefix} {name}, via {self.database.dsn}"


async def test_quickstart():
    """The three claims the quickstart makes, in order."""
    root = Context()

    greeter = await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    assert "greeter" not in root, "it did not wait for its dependency"

    database = await root.plugin(provide(Database, "database"))
    await greeter
    assert root.greeter.hello("world") == "hello world, via sqlite://"

    await database.dispose()
    assert "greeter" not in root, "it did not stop with its dependency"


async def test_what_await_on_a_mount_waits_for():
    """The three rows of the README table."""
    from plugkit import FiberState
    import time

    root = Context()

    async def slow(ctx, config=None):
        await asyncio.sleep(0.2)

    started = time.monotonic()
    await root.plugin(slow)
    assert time.monotonic() - started >= 0.19, "await did not block on a slow plugin"

    root = Context()
    started = time.monotonic()
    greeter = await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    assert time.monotonic() - started < 0.05
    assert greeter.state is FiberState.PENDING

    await root.plugin(provide(Database, "database"))
    assert greeter.state is FiberState.LOADING, "the dependent was already active"
    await greeter
    assert greeter.state is FiberState.ACTIVE


async def test_dispose_needs_no_extra_wait():
    """`await fiber.dispose()` covers the cascade to dependents."""
    root = Context()
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    database = await root.plugin(provide(Database, "database"))
    await settle()
    assert "greeter" in root

    await database.dispose()
    assert "greeter" not in root, "dispose returned before the cascade finished"


async def test_awaiting_a_pending_plugin_returns():
    """The quickstart awaits a mount whose dependency is missing."""
    from plugkit import FiberState

    root = Context()
    fiber = await asyncio.wait_for(
        root.plugin(provide(Greeter, "greeter", needs=["database"])), timeout=1.0
    )
    assert fiber.state is FiberState.PENDING


async def test_swapping_an_implementation_rebuilds_dependents():
    """The comparison table's third row."""

    class OtherDatabase(Database):
        pass

    root = Context()
    database = await root.plugin(provide(Database, "database"))
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()
    first = root.greeter
    assert isinstance(first.database, Database)

    await database.dispose()
    await settle()
    await root.plugin(provide(OtherDatabase, "database"))
    await settle()

    assert root.greeter is not first, "the dependent was not rebuilt"
    assert isinstance(root.greeter.database, OtherDatabase)


# ── "The one idea", steps 1-5 ─────────────────────────────────────────────


class Server:
    """Step 1. Ordinary Python. plugkit does not know what a route is."""

    def __init__(self):
        self.routes = {}

    def add_route(self, path, handler):
        self.routes[path] = handler

    def remove_route(self, path):
        self.routes.pop(path, None)


async def test_step3_inject_is_a_precondition():
    """Mounted before its dependency exists, the plugin waits rather than fails."""
    from plugkit import FiberState

    def admin_api(ctx, config=None):
        ctx.server.add_route("/admin", lambda: "admin page")

    admin_api.inject = ["server"]

    root = Context()
    fiber = root.plugin(admin_api)
    await settle()
    assert fiber.state is FiberState.PENDING
    assert "server" not in root

    await root.plugin(provide(Server, "server"))
    await settle()
    assert fiber.state is FiberState.ACTIVE
    assert list(root.server.routes) == ["/admin"]


async def test_step3_ctx_server_is_just_the_instance():
    """No wrapper, no proxy semantics the reader has to learn."""
    root = Context()
    await root.plugin(provide(Server, "server"))
    await settle()
    assert isinstance(root.server, Server)
    assert isinstance(root.server.routes, dict)
    assert root.server is root["server"]


async def test_step4_the_route_survives_unload():
    """The claim the whole section rests on."""

    def admin_api(ctx, config=None):
        ctx.server.add_route("/admin", lambda: "admin page")

    admin_api.inject = ["server"]

    root = Context()
    await root.plugin(provide(Server, "server"))
    fiber = await root.plugin(admin_api)
    await settle()
    assert list(root.server.routes) == ["/admin"]

    await fiber.dispose()
    await settle()
    assert list(root.server.routes) == ["/admin"], "the README's problem does not reproduce"


async def test_step5_returning_the_undo_fixes_it():
    def admin_api(ctx, config=None):
        ctx.server.add_route("/admin", lambda: "admin page")
        return lambda: ctx.server.remove_route("/admin")

    admin_api.inject = ["server"]

    root = Context()
    await root.plugin(provide(Server, "server"))
    fiber = await root.plugin(admin_api)
    await settle()
    assert list(root.server.routes) == ["/admin"]

    await fiber.dispose()
    await settle()
    assert list(root.server.routes) == []


async def test_step5_effects_run_in_reverse():
    order = []

    def admin_api(ctx, config=None):
        def route(path, handler):
            def install():
                ctx.server.add_route(path, handler)
                order.append(f"add {path}")
                return lambda: (ctx.server.remove_route(path), order.append(f"remove {path}"))
            return install

        ctx.effect(route("/admin", lambda: "admin"))
        ctx.effect(route("/health", lambda: "ok"))

    admin_api.inject = ["server"]

    root = Context()
    await root.plugin(provide(Server, "server"))
    fiber = await root.plugin(admin_api)
    await settle()
    assert sorted(root.server.routes) == ["/admin", "/health"]

    await fiber.dispose()
    await settle()
    assert root.server.routes == {}
    assert order == ["add /admin", "add /health", "remove /health", "remove /admin"]


async def test_unload_also_happens_when_the_dependency_is_replaced():
    """The README claims the fiber covers unload paths a try/finally does not."""
    removed = []

    def admin_api(ctx, config=None):
        ctx.server.add_route("/admin", lambda: "admin page")
        return lambda: removed.append("cleaned")

    admin_api.inject = ["server"]

    root = Context()
    server_fiber = await root.plugin(provide(Server, "server"))
    await root.plugin(admin_api)
    await settle()
    assert removed == []

    await server_fiber.dispose()          # nobody called dispose on admin_api
    await settle()
    assert removed == ["cleaned"], "the undo did not run when the dependency left"


# ── "Writing a plugin" ────────────────────────────────────────────────────


async def test_a_plugin_takes_exactly_two_parameters():
    from plugkit import FiberState

    def one(ctx):
        pass

    def two(ctx, config):
        pass

    def three(ctx, config, extra):
        pass

    states = {}
    for fn in (one, two, three):
        root = Context()
        fiber = root.plugin(fn)
        await settle()
        states[fn.__name__] = fiber.state

    assert states["one"] is FiberState.FAILED
    assert states["two"] is FiberState.ACTIVE
    assert states["three"] is FiberState.FAILED


async def test_the_config_parameter_is_per_mount():
    """One shared ctx.config could not express two ports for two live mounts."""
    seen = []

    def server(ctx, config=None):
        seen.append(config["port"])

    root = Context()
    await root.plugin(server, {"port": 8080})
    await root.plugin(server, {"port": 9090})
    await settle()
    assert seen == [8080, 9090]


async def test_config_parameter_and_ctx_config_are_unrelated():
    from plugkit import ConfigService

    observed = {}

    def probe(ctx, config=None):
        observed["parameter"] = config
        observed["service"] = ctx.config.get("app.name")

    probe.inject = ["config"]

    root = Context()
    await root.plugin(ConfigService, {"dict": {"app": {"name": "shared"}}})
    await root.plugin(probe, {"port": 8080})
    await settle()

    assert observed["parameter"] == {"port": 8080}
    assert observed["service"] == "shared"


async def test_reading_a_service_not_in_inject_raises():
    """Point 2 of the `inject` list."""
    errors = []

    def sneaky(ctx, config=None):
        try:
            ctx.server
        except AttributeError as exc:
            errors.append(str(exc))

    root = Context()
    await root.plugin(provide(Server, "server"))
    await root.plugin(sneaky)
    await settle()
    assert errors and "without inject" in errors[0]


async def test_the_typed_disposer_example_runs():
    """The README's disposer example, annotated and using @plugin."""
    from typing import Any, Callable, Protocol

    from plugkit import plugin

    class TypedServer:
        def __init__(self) -> None:
            self.routes: dict[str, Callable[[], str]] = {}

        def add_route(self, path: str, handler: Callable[[], str]) -> None:
            self.routes[path] = handler

        def remove_route(self, path: str) -> None:
            self.routes.pop(path, None)

    class ServerDeps(Protocol):
        server: TypedServer

    @plugin
    def admin_api(ctx: ServerDeps, config: Any = None) -> Callable[[], None]:
        ctx.server.add_route("/admin", lambda: "admin page")
        return lambda: ctx.server.remove_route("/admin")

    assert admin_api["inject"] == ["server"], admin_api["inject"]

    root = Context()
    await root.plugin(provide(TypedServer, "server"))
    fiber = await root.plugin(admin_api)
    await settle()
    assert list(root.server.routes) == ["/admin"]

    await fiber.dispose()
    await settle()
    assert list(root.server.routes) == []


async def test_plugin_returns_pending_before_awaiting():
    """The README says the dependency check happens inside plugin(), not await."""
    from plugkit import FiberState

    root = Context()
    fiber = root.plugin(provide(Greeter, "greeter", needs=["database"]))
    assert fiber.state is FiberState.PENDING, "the check did not run during plugin()"
    assert fiber.inertia is None, "a load was scheduled despite a missing dependency"


async def test_the_typed_effects_example_runs():
    """The README's multi-disposer example, annotated."""
    from typing import Any, Callable, Protocol

    from plugkit import plugin

    class TypedServer:
        def __init__(self) -> None:
            self.routes: dict[str, Callable[[], str]] = {}

        def add_route(self, path: str, handler: Callable[[], str]) -> None:
            self.routes[path] = handler

        def remove_route(self, path: str) -> None:
            self.routes.pop(path, None)

    class ServerDeps(Protocol):
        server: TypedServer
        def effect(self, execute: Any, label: str = ...) -> Any: ...

    Handler = Callable[[], str]
    Disposer = Callable[[], None]

    @plugin
    def admin_api(ctx: ServerDeps, config: Any = None) -> None:
        def route(path: str, handler: Handler) -> Callable[[], Disposer]:
            def install() -> Disposer:
                ctx.server.add_route(path, handler)
                return lambda: ctx.server.remove_route(path)
            return install

        ctx.effect(route("/admin", lambda: "admin"))
        ctx.effect(route("/health", lambda: "ok"))

    assert admin_api["inject"] == ["server"], admin_api["inject"]

    root = Context()
    await root.plugin(provide(TypedServer, "server"))
    fiber = await root.plugin(admin_api)
    await settle()
    assert sorted(root.server.routes) == ["/admin", "/health"]

    await fiber.dispose()
    await settle()
    assert root.server.routes == {}


# ── "What each call returns" ─────────────────────────────────────────────


async def test_provide_returns_a_description_and_runs_nothing():
    p = provide(Database, "database")
    assert isinstance(p, dict)
    assert sorted(p) == ["apply", "factory", "inject", "name", "provides"]
    assert not hasattr(p, "__await__")


async def test_plugin_returns_a_fiber_synchronously():
    from plugkit import Fiber, FiberState

    root = Context()
    fiber = root.plugin(provide(Database, "database"))

    assert isinstance(fiber, Fiber)
    assert hasattr(fiber, "__await__")
    assert fiber.state is FiberState.LOADING
    assert "database" not in root

    await fiber
    assert fiber.state is FiberState.ACTIVE
    assert "database" in root


async def test_ctx_provide_is_the_other_one():
    """The context method registers immediately and returns a disposer."""
    captured = {}

    def my_plugin(ctx, config=None):
        captured["disposer"] = ctx.provide("greeting", "hello")

    root = Context()
    await root.plugin(my_plugin)
    assert root.greeting == "hello"
    assert callable(captured["disposer"])
