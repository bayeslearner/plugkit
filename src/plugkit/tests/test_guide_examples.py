"""Run the examples from the guide chapters and assert what the prose claims.

Same reason as `test_readme_examples.py`: a documented API that does not exist is
the worst documentation bug, and the only defence is running what you wrote.
"""

import asyncio

import pytest

from plugkit import (
    Context,
    FiberState,
    PointsService,
    Service,
    plugin,
    provide,
)


async def settle(n=15):
    for _ in range(n):
        await asyncio.sleep(0)


# ── chapter 4: extension points ──────────────────────────────────────────


async def test_a_contribution_dies_with_its_contributor():
    """The chapter's first claim: unload the contributor, the route goes."""
    root = Context()
    await root.plugin(PointsService)

    def admin(ctx, config=None):
        ctx.points.add("http.routes", "admin_page", key="/admin", order=10)

    admin.inject = ["points"]
    fiber = await root.plugin(admin)
    await settle()
    assert root.points.all("http.routes") == ["admin_page"]

    await fiber.dispose()
    await settle()
    assert root.points.all("http.routes") == [], "the route outlived its plugin"


async def test_the_reading_api_the_chapter_shows():
    root = Context()
    await root.plugin(PointsService)
    root.points.add("http.routes", "admin", key="/admin", method="GET")
    root.points.add("http.routes", "submit", key="/submit", method="POST")

    assert root.points.all("http.routes") == ["admin", "submit"]
    assert root.points.get("http.routes", "/admin") == "admin"
    assert root.points.where("http.routes", method="GET") == ["admin"]
    assert root.points.names() == ["http.routes"]


async def test_on_change_wakes_the_consumer_and_dies_with_it():
    root = Context()
    await root.plugin(PointsService)
    rebuilds = []

    def router(ctx, config=None):
        ctx.points.on_change("http.routes", lambda: rebuilds.append(1))

    router.inject = ["points"]
    fiber = await root.plugin(router)
    await settle()

    root.points.add("http.routes", "a")
    assert rebuilds == [1]

    await fiber.dispose()
    root.points.add("http.routes", "b")
    assert rebuilds == [1], "the consumer unloaded and was still called"


async def test_last_does_not_assume_reverse_unload_order():
    """The chapter's claim about why `last()` exists rather than a saved slot."""
    root = Context()
    await root.plugin(PointsService)

    older = root.points.add("tools.approvers", "old_approver")
    root.points.add("tools.approvers", "new_approver")

    older()  # the *older* registration goes first
    assert root.points.last("tools.approvers") == "new_approver"


class Router(Service):
    """The chapter's `Service` example, verbatim."""

    provide = "router"
    inject = ["points"]

    def add(self, path: str, handler: object):
        return self.ctx.points.add("http.routes", handler, key=path, unique=True)

    def resolve(self, path: str):
        return self.ctx.points.get("http.routes", path)


async def test_a_service_holding_its_collection_in_a_point():
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(Router)
    await settle()

    def admin(ctx, config=None):
        ctx.router.add("/admin", "admin_page")

    admin.inject = ["router"]
    fiber = await root.plugin(admin)
    await settle()
    assert root.router.resolve("/admin") == "admin_page"

    await fiber.dispose()
    await settle()
    assert root.router.resolve("/admin") is None, "rebinding did not reach the caller"


async def test_unique_rejects_a_second_route_on_one_path():
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(Router)
    await settle()

    root.router.add("/admin", "first")
    with pytest.raises(ValueError):
        root.router.add("/admin", "second")


async def test_a_callable_held_as_service_data_is_not_rebound():
    """The hazard the chapter warns about, asserted rather than described."""

    class Approvals(Service):
        provide = "approvals"

        def __init__(self, ctx, config=None):
            super().__init__(ctx)
            self._answer = lambda question: f"answered {question}"

    root = Context()
    await root.plugin(Approvals)

    seen = []

    def caller(ctx, config=None):
        seen.append(ctx.approvals._answer("now?"))

    caller.inject = ["approvals"]
    await root.plugin(caller)
    await settle()
    assert seen == ["answered now?"], "the stored callable was rebound as a method"


# ── chapter 5: config and reactivity ─────────────────────────────────────


async def test_a_runtime_set_outranks_a_later_load(tmp_path):
    from plugkit import ConfigService

    other = tmp_path / "other.yml"
    other.write_text("http:\n  host: elsewhere\n")

    root = Context()
    await root.plugin(ConfigService, {"dict": {"http": {"timeout": 30}}})
    await settle()

    assert root.config.get("http.timeout") == 30
    assert root.config.get("http.retries", 3) == 3

    root.config.set("http.timeout", 60)
    root.config.load_yaml(str(other))
    assert root.config.get("http.timeout") == 60
    assert root.config.get("http.host") == "elsewhere"


async def test_an_effect_reruns_only_for_the_key_it_read():
    from plugkit import ConfigService, ReactiveService

    timeouts, hosts = [], []
    root = Context()
    await root.plugin(ReactiveService)
    await root.plugin(ConfigService, {"dict": {"http": {"timeout": 30, "host": "a"}}})
    await settle()

    def watcher(ctx, config=None):
        ctx.reactive.effect(lambda: timeouts.append(ctx.config.get("http.timeout")))
        ctx.reactive.effect(lambda: hosts.append(ctx.config.get("http.host")))

    watcher.inject = ["config", "reactive"]
    await root.plugin(watcher)
    await settle()

    root.config.set("http.timeout", 60)
    assert timeouts == [30, 60]
    assert hosts == ["a"], "an unrelated reader re-ran"


async def test_config_override_restores(tmp_path):
    from plugkit import ConfigService

    root = Context()
    await root.plugin(ConfigService, {"dict": {"http": {"timeout": 30}}})
    await settle()

    with root.config.override({"http": {"timeout": 1}}):
        assert root.config.get("http.timeout") == 1
    assert root.config.get("http.timeout") == 30


# ── chapter 7: testing ───────────────────────────────────────────────────


class FakeDatabase:
    def __init__(self):
        self.dsn = "fake://"


class Greeter:
    def __init__(self, database):
        self.database = database

    def hello(self, name):
        return f"hello {name}"


def test_a_component_is_testable_with_no_kernel():
    """The chapter's opening claim, and the reason provide() exists."""
    assert Greeter(database=FakeDatabase()).hello("world") == "hello world"


async def test_substituting_a_dependency_is_mounting_a_different_class():
    root = Context()
    await root.plugin(provide(FakeDatabase, "database"))
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    assert root.greeter.database.dsn == "fake://"


async def test_observing_a_failure_without_awaiting():
    def might_fail(ctx, config=None):
        raise RuntimeError("no")

    root = Context()
    fiber = root.plugin(might_fail)

    with pytest.raises(RuntimeError):
        await fiber                      # waits for the transition, then re-raises
    assert fiber.state is FiberState.FAILED


async def test_a_failed_load_takes_three_ticks():
    """Why the guide says not to count ticks: LOADING -> UNLOADING -> FAILED."""
    seen = []
    root = Context()
    fiber = root.plugin(lambda ctx, config=None: (_ for _ in ()).throw(RuntimeError("no")))
    for _ in range(4):
        seen.append(fiber.state.name)
        await asyncio.sleep(0)
    assert seen == ["LOADING", "LOADING", "UNLOADING", "FAILED"], seen


# ── chapter 7: asking the system what it is doing ────────────────────────


async def test_describe_names_what_a_pending_plugin_waits_for():
    """The chapter's claim about the line that saves time."""
    from plugkit import describe

    root = Context()
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    def by_name(snapshot, name):
        return next(f for f in snapshot["fibers"] if f["name"] == name)

    entry = by_name(describe(root), "greeter")
    assert entry["state"] == "PENDING"
    assert entry["missing"] == ["database"]


async def test_the_diagnostics_example_from_the_chapter():
    from plugkit import DIAGNOSTICS, describe

    class Pool:
        size = 4

    root = Context()
    await root.plugin(PointsService)

    def database(ctx, config=None):
        pool = Pool()
        ctx.provide("database", pool)
        ctx.points.add(DIAGNOSTICS, lambda: {"pool_size": pool.size}, key="database")

    database.inject = ["points"]
    await root.plugin(database)
    await settle()

    assert describe(root)["diagnostics"]["database"] == {"pool_size": 4}
