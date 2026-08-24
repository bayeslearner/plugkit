"""Config: dependency-injector for loading, a Signal for propagation."""

import asyncio

import pytest

from plugkit import Context
from plugkit.services.config import ConfigService
from plugkit.services.reactive import ReactiveService


async def settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0)


async def boot(**config):
    root = Context()
    await root.plugin(ReactiveService)
    await root.plugin(ConfigService, config or None)
    await settle()
    return root


async def test_dotted_get_with_default():
    root = await boot(dict={"http": {"timeout": 30}})
    assert root.config.get("http.timeout") == 30
    assert root.config.get("http.retries", 3) == 3
    assert root.config.get("nope.nope.nope") is None


async def test_require_raises_on_missing():
    root = await boot(dict={"a": 1})
    assert root.config.require("a") == 1
    with pytest.raises(KeyError, match="b.c"):
        root.config.require("b.c")


async def test_set_wakes_only_readers_of_that_value():
    timeouts, retries = [], []
    root = await boot(dict={"http": {"timeout": 30, "retries": 1}})

    def watcher(ctx, config=None):
        ctx.reactive.effect(lambda: timeouts.append(ctx.config.get("http.timeout")))
        ctx.reactive.effect(lambda: retries.append(ctx.config.get("http.retries")))

    watcher.inject = ["config", "reactive"]
    await root.plugin(watcher)
    await settle()
    assert (timeouts, retries) == ([30], [1])

    root.config.set("http.timeout", 60)
    assert timeouts == [30, 60]
    assert retries == [1], "an unrelated reader re-ran"


async def test_set_does_not_mutate_in_place():
    """Signal.set compares identity; an in-place write would notify nobody."""
    root = await boot(dict={"a": {"b": 1}})
    before = root.config.all()
    root.config.set("a.b", 2)
    assert before["a"]["b"] == 1, "the previous snapshot was mutated"
    assert root.config.get("a.b") == 2


async def test_peek_does_not_register_a_dependency():
    seen = []
    root = await boot(dict={"x": 1})

    def watcher(ctx, config=None):
        ctx.reactive.effect(lambda: seen.append(ctx.config.peek("x")))

    watcher.inject = ["config", "reactive"]
    await root.plugin(watcher)
    await settle()
    assert seen == [1]
    root.config.set("x", 2)
    assert seen == [1], "peek() registered a reactive dependency"


async def test_yaml_layers_then_dict_wins(tmp_path):
    base = tmp_path / "base.yml"
    base.write_text("http:\n  timeout: 30\n  host: base\n")
    root = await boot(yaml=str(base), dict={"http": {"timeout": 99}})
    assert root.config.get("http.timeout") == 99
    assert root.config.get("http.host") == "base", "deep merge lost a sibling key"


async def test_missing_yaml_is_skipped_unless_required(tmp_path):
    root = await boot(yaml=str(tmp_path / "nope.yml"))
    assert root.config.all() == {}
    with pytest.raises(FileNotFoundError):
        root.config.load_yaml(str(tmp_path / "nope.yml"), required=True)


async def test_config_reads_survive_plugin_reload():
    """A config change must not reload the plugin — that is the whole point."""
    applies = []
    root = await boot(dict={"n": 1})

    def watcher(ctx, config=None):
        applies.append("apply")
        ctx.reactive.effect(lambda: ctx.config.get("n"))

    watcher.inject = ["config", "reactive"]
    await root.plugin(watcher)
    await settle()
    assert applies == ["apply"]

    for value in (2, 3, 4):
        root.config.set("n", value)
    await settle()
    assert applies == ["apply"], "a config change reloaded the plugin"
