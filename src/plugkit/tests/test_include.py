"""Port of packages/include/tests/patch.spec.ts."""

import os

import pytest

from plugkit.cordis import Context, Loader
from plugkit.cordis.include import Include

from .conftest import Mock, sleep
from .loader_utils import install

FIXTURES = os.path.dirname(os.path.abspath(__file__)) + "/fixtures"


def get_fixture(name):
    return os.path.join(FIXTURES, name)


async def create_include(ctx, **config):
    ctx.loader.modules["__cordis_include__"] = Include
    await ctx.loader.create({"name": "__cordis_include__", "config": {"path": get_fixture("base.yml"), **config}})
    for _ in range(50):
        await sleep()


async def test_should_load_without_patches():
    ctx = Context()
    await install(ctx)
    await create_include(ctx)
    assert ctx.bail("test/get-value") == "default"


async def test_should_disable_an_entry_via_patch():
    ctx = Context()
    await install(ctx)
    await create_include(ctx, patches=[{"id": "inner", "disabled": True}])
    # inner plugin should be disabled
    assert ctx.bail("test/get-value") is None


async def test_should_override_config_via_patch():
    ctx = Context()
    await install(ctx)
    await create_include(ctx, patches=[{"id": "inner", "config": {"custom": True}}])
    # Plugin should still load (config override doesn't break it)
    assert ctx.bail("test/get-value") == "default"


async def test_should_warn_on_name_mismatch_and_skip_patch():
    ctx = Context()
    await install(ctx)
    warned = []

    async def check():
        return any("mismatch" in str(m.args[0]) for m in ctx.logger.buffer if m.type == "warn")

    await create_include(ctx, patches=[{"id": "inner", "name": "wrong-name", "disabled": True}])
    # Plugin should still be active (patch was skipped due to name mismatch)
    assert ctx.bail("test/get-value") == "default"


async def test_should_warn_on_non_existent_id():
    ctx = Context()
    await install(ctx)
    await create_include(ctx, patches=[{"id": "nonexistent", "disabled": True}])
    # Should still work, patch just gets warned and ignored
    assert ctx.bail("test/get-value") == "default"


async def test_should_insert_entries_into_root_group():
    ctx = Context()
    await install(ctx)
    await create_include(ctx, patches=[{"insert": [{"name": "plugkit.tests.fixtures.extra_plugin"}]}])
    assert ctx.bail("test/get-extra") == "extra"
    # Original plugin should still work
    assert ctx.bail("test/get-value") == "default"


async def test_should_insert_entries_into_a_specific_group():
    ctx = Context()
    await install(ctx)
    await create_include(ctx, patches=[{"id": "group", "insert": [{"name": "plugkit.tests.fixtures.extra_plugin"}]}])
    assert ctx.bail("test/get-extra") == "extra"


async def test_should_warn_when_inserting_into_a_non_group_entry():
    ctx = Context()
    await install(ctx)
    await create_include(ctx, patches=[{"id": "timer", "insert": [{"name": "plugkit.tests.fixtures.extra_plugin"}]}])
    # extra plugin should NOT be loaded (timer is not a group)
    assert ctx.bail("test/get-extra") is None


async def test_should_apply_multiple_patches():
    ctx = Context()
    await install(ctx)
    await create_include(
        ctx,
        patches=[
            {"id": "inner", "disabled": True},
            {"insert": [{"name": "plugkit.tests.fixtures.extra_plugin"}]},
        ],
    )
    # inner disabled, extra loaded
    assert ctx.bail("test/get-value") is None
    assert ctx.bail("test/get-extra") == "extra"


async def test_should_validate_name_consistency():
    ctx = Context()
    await install(ctx)
    await create_include(ctx, patches=[{"id": "inner", "name": "plugkit.tests.fixtures.test_plugin", "disabled": True}])
    # Name matches, so patch should apply and inner should be disabled
    assert ctx.bail("test/get-value") is None
