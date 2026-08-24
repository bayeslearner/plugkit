"""Port of packages/loader/tests/isolate.spec.ts."""

from plugkit.cordis import Context, FiberState, Service

from .conftest import Mock, sleep
from .loader_utils import install


class BarService(Service):
    def __init__(self, ctx, config):
        super().__init__(ctx, "bar")


async def test_service_isolation_basic():
    root = Context()
    dispose = Mock()

    loader = await install(root)

    foo = loader.mock("foo", lambda ctx, config: dispose)
    foo.inject = ["bar"]

    bar = loader.mock("bar", BarService)

    provider = await loader.create({"name": "bar"})
    injector = await loader.create({"name": "foo"})

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0

    # add isolate on injector (relevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(injector, {"isolate": {"bar": True}})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 1

    # add isolate on injector (irrelevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(injector, {"isolate": {"bar": True, "qux": True}})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0

    # remove isolate on injector (relevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(injector, {"isolate": {"qux": True}})

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0

    # remove isolate on injector (irrelevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(injector, {"isolate": None})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0

    # add isolate on provider (relevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(provider, {"isolate": {"bar": True}})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 1

    # add isolate on provider (irrelevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(provider, {"isolate": {"bar": True, "qux": True}})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0

    # remove isolate on provider (relevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(provider, {"isolate": {"qux": True}})

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0

    # remove isolate on provider (irrelevant)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(provider, {"isolate": None})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0


async def test_service_isolation_realm():
    root = Context()
    dispose = Mock()

    loader = await install(root)

    foo = loader.mock("foo", lambda ctx, config: dispose)
    foo.inject = ["bar"]

    def bar_apply(ctx, config=None):
        config = config or {}
        ctx.provide("bar", config)

    bar = loader.mock("bar", bar_apply)

    alpha = await loader.create(
        {
            "name": "@cordisjs/plugin-group",
            "isolate": {"bar": True},
            "config": [{"name": "bar", "config": {"value": "alpha"}}],
        }
    )

    beta = await loader.create(
        {
            "name": "@cordisjs/plugin-group",
            "isolate": {"bar": "beta"},
            "config": [{"name": "bar", "config": {"value": "beta"}}],
        }
    )

    await sleep()
    assert root.registry.get(bar).fibers.length == 2
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0

    # update isolate group (no change)
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(alpha, {"isolate": {"bar": True}})

    await sleep()
    assert root.registry.get(bar).fibers.length == 2
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0

    # realm reference
    foo.reset_calls()
    dispose.reset_calls()
    nested1 = await loader.create({"name": "foo"}, alpha)

    nested2 = await loader.create({"name": "foo", "isolate": {"bar": "beta"}}, alpha)

    nested3 = await loader.create({"name": "foo", "isolate": {"bar": True}}, alpha)

    await sleep()
    assert len(foo.calls) == 2
    assert len(dispose.calls) == 0
    fiber1 = loader.expect_fiber(nested1)
    assert fiber1.ctx.get("bar")["value"] == "alpha"
    assert fiber1.state == FiberState.ACTIVE
    fiber2 = loader.expect_fiber(nested2)
    assert fiber2.ctx.get("bar")["value"] == "beta"
    assert fiber2.state == FiberState.ACTIVE
    fiber3 = loader.expect_fiber(nested3)
    assert fiber3.ctx.get("bar") is None
    assert fiber3.state == FiberState.PENDING


async def test_service_isolation_nested_realms():
    root = Context()
    dispose = Mock()

    loader = await install(root)

    foo = loader.mock("foo", lambda ctx, config: dispose)
    foo.inject = ["bar"]

    def bar_apply(ctx, config=None):
        config = config or {}
        ctx.provide("bar", config)

    loader.mock("bar", bar_apply)

    outer = await loader.create({"name": "@cordisjs/plugin-group", "config": []})

    inner = await loader.create(
        {"name": "@cordisjs/plugin-group", "isolate": {"bar": "custom"}, "config": []}, outer
    )

    await loader.create({"name": "bar", "config": {"value": "custom"}}, inner)

    alpha = await loader.create({"name": "foo", "isolate": {"bar": "custom"}})

    beta = await loader.create({"name": "foo"}, inner)

    await sleep()
    fiber1 = loader.expect_fiber(alpha)
    fiber2 = loader.expect_fiber(beta)
    assert fiber1.ctx.get("bar")["value"] == "custom"
    assert fiber2.ctx.get("bar")["value"] == "custom"

    foo.reset_calls()
    dispose.reset_calls()

    await loader.update(outer, {"isolate": {"bar": "custom"}})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0

    foo.reset_calls()
    dispose.reset_calls()

    await loader.update(outer, {"isolate": {}})

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0


async def test_service_isolation_change_provider():
    root = Context()
    dispose = Mock()

    loader = await install(root)

    foo = loader.mock("foo", lambda ctx, config: dispose)
    foo.inject = ["bar"]

    def bar_apply(ctx, config=None):
        config = config or {}
        ctx.provide("bar", config)

    loader.mock("bar", bar_apply)

    await loader.create({"name": "bar", "isolate": {"bar": "alpha"}, "config": {"value": "alpha"}})

    await loader.create({"name": "bar", "isolate": {"bar": "beta"}, "config": {"value": "beta"}})

    group = await loader.create(
        {"name": "@cordisjs/plugin-group", "isolate": {"bar": "alpha"}, "config": []}
    )

    id_ = await loader.create({"name": "foo"}, group)

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0
    fiber = loader.expect_fiber(id_)
    assert fiber.ctx.get("bar")["value"] == "alpha"

    foo.reset_calls()
    dispose.reset_calls()

    await loader.update(group, {"isolate": {"bar": "beta"}})

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 1
    assert fiber.ctx.get("bar")["value"] == "beta"


async def test_service_isolation_change_injector():
    root = Context()
    dispose = Mock()

    loader = await install(root)

    foo = loader.mock("foo", lambda ctx, config: dispose)
    foo.inject = ["bar"]

    def bar_apply(ctx, config=None):
        config = config or {}
        ctx.provide("bar", config)

    bar = loader.mock("bar", bar_apply)

    alpha = await loader.create({"name": "foo", "isolate": {"bar": "alpha"}})

    beta = await loader.create({"name": "foo", "isolate": {"bar": "beta"}})

    group = await loader.create(
        {"name": "@cordisjs/plugin-group", "isolate": {"bar": "alpha"}, "config": []}
    )

    inner = await loader.create({"name": "bar"}, group)

    await loader.expect_fiber(inner)
    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0
    fiber1 = loader.expect_fiber(alpha)
    assert fiber1.ctx.get("bar") is not None
    fiber2 = loader.expect_fiber(beta)
    assert fiber2.ctx.get("bar") is None

    foo.reset_calls()
    dispose.reset_calls()

    await loader.update(group, {"isolate": {"bar": "beta"}})

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 1
    assert fiber1.ctx.get("bar") is None
    assert fiber2.ctx.get("bar") is not None


async def test_service_isolation_transfer():
    root = Context()
    dispose = Mock()

    loader = await install(root)

    foo = loader.mock("foo", lambda ctx, config: dispose)
    foo.inject = ["bar"]

    bar = loader.mock("bar", BarService)

    group = await loader.create(
        {"name": "@cordisjs/plugin-group", "isolate": {"bar": True}, "config": []}
    )

    provider = await loader.create({"name": "bar"})
    injector = await loader.create({"name": "foo"})

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0

    # transfer injector into group
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(injector, {}, group)

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 1

    # transfer provider into group
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(provider, {}, group)

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0

    # transfer injector out of group
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(injector, {}, None)

    await sleep()
    assert len(foo.calls) == 0
    assert len(dispose.calls) == 1

    # transfer provider out of group
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(provider, {}, None)

    await sleep()
    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0
