"""Port of packages/loader/tests/group.spec.ts."""

from plugkit.cordis import Context

from .conftest import Mock, sleep
from .loader_utils import install


async def test_group_basic_support():
    root = Context()
    dispose = Mock()

    loader = await install(root)
    foo = loader.mock("foo", lambda ctx, config: dispose)

    outer = await loader.create(
        {"name": "@cordisjs/plugin-group", "group": True, "config": [{"name": "foo"}]}
    )

    inner = await loader.create(
        {"name": "@cordisjs/plugin-group", "group": True, "config": [{"name": "foo"}]}, outer
    )

    await sleep()
    loader.expect_fiber(outer)
    loader.expect_fiber(inner)
    assert len(foo.calls) == 2
    assert len(dispose.calls) == 0
    assert len(list(loader.entries())) == 4

    # disable inner
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(inner, {"disabled": True})

    assert len(foo.calls) == 0
    assert len(dispose.calls) == 1
    assert len(list(loader.entries())) == 4

    # disable outer
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(outer, {"disabled": True})

    assert len(foo.calls) == 0
    assert len(dispose.calls) == 1
    assert len(list(loader.entries())) == 4

    # enable inner
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(inner, {"disabled": None})

    assert len(foo.calls) == 0  # outer is still disabled
    assert len(dispose.calls) == 0
    assert len(list(loader.entries())) == 4

    # enable outer
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(outer, {"disabled": None})

    await sleep()
    assert len(foo.calls) == 2
    assert len(dispose.calls) == 0
    assert len(list(loader.entries())) == 4


async def test_group_transfer():
    root = Context()
    dispose = Mock()

    loader = await install(root)
    foo = loader.mock("foo", lambda ctx, config: dispose)

    id_ = await loader.create({"name": "foo"})

    alpha = await loader.create({"name": "@cordisjs/plugin-group", "group": True, "config": []})

    beta = await loader.create(
        {"name": "@cordisjs/plugin-group", "group": True, "disabled": True, "config": []}, alpha
    )

    gamma = await loader.create({"name": "@cordisjs/plugin-group", "group": True, "config": []}, beta)

    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0
    assert len(list(loader.entries())) == 4

    # enabled -> enabled
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(id_, {}, alpha)

    assert len(foo.calls) == 0
    assert len(dispose.calls) == 0
    assert len(list(loader.entries())) == 4

    # enabled -> disabled
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(id_, {}, beta)

    assert len(foo.calls) == 0
    assert len(dispose.calls) == 1
    assert len(list(loader.entries())) == 4

    # disabled -> disabled
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(id_, {}, gamma)

    assert len(foo.calls) == 0  # outer is still disabled
    assert len(dispose.calls) == 0
    assert len(list(loader.entries())) == 4

    # disabled -> enabled
    foo.reset_calls()
    dispose.reset_calls()
    await loader.update(id_, {}, None)

    assert len(foo.calls) == 1
    assert len(dispose.calls) == 0
    assert len(list(loader.entries())) == 4


async def test_group_intercept():
    root = Context()
    callback = Mock()

    loader = await install(root)
    loader.mock("foo", lambda ctx, config: callback(ctx._effective_intercept()))

    outer = await loader.create(
        {
            "name": "@cordisjs/plugin-group",
            "group": True,
            "intercept": {"foo": {"a": 1}},
            "config": [],
        }
    )

    inner = await loader.create(
        {
            "name": "@cordisjs/plugin-group",
            "group": True,
            "intercept": {"foo": {"b": 2}},
            "config": [],
        },
        outer,
    )

    id_ = await loader.create({"name": "foo", "intercept": {"foo": {"c": 3}}}, inner)

    await sleep()
    assert len(callback.calls) == 1
    intercept = callback.calls[0]["args"][0]
    assert intercept["foo"] == {"c": 3}
    parent = intercept
    # walk the context chain: each entry contributes one intercept map
    ctx = loader.expect_fiber(id_).ctx
    maps = []
    obj = ctx
    while obj is not None:
        if "_intercept" in obj.__dict__:
            maps.append(obj.__dict__["_intercept"])
        obj = obj.__dict__.get("_parent")
    assert maps[0]["foo"] == {"c": 3}
    assert maps[1]["foo"] == {"b": 2}
    assert maps[2]["foo"] == {"a": 1}
