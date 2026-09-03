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


# ── 07: change notification is a watcher, not an effect ──────────────────


async def test_watch_calls_back_with_next_and_prev():
    """The shape the reference settles on: `watch(cb(next, prev)) -> disposer`."""
    root = await boot(dict={"http": {"timeout": 30}})
    seen = []
    root.config.watch("http.timeout", lambda next_, prev: seen.append((next_, prev)))

    root.config.set("http.timeout", 60)
    await settle()
    assert seen == [(60, 30)]

    root.config.set("http.timeout", 90)
    await settle()
    assert seen == [(60, 30), (90, 60)]


async def test_watch_does_not_fire_on_registration():
    """The caller just read the value; a first application it did not ask for is a surprise."""
    root = await boot(dict={"http": {"timeout": 30}})
    seen = []
    root.config.watch("http.timeout", lambda next_, prev: seen.append(next_))
    await settle()
    assert seen == []


async def test_watch_is_per_key():
    root = await boot(dict={"a": 1, "b": 2})
    seen = []
    root.config.watch("a", lambda next_, prev: seen.append(next_))

    root.config.set("b", 20)
    await settle()
    assert seen == [], "a write to another key woke this watcher"

    root.config.set("a", 10)
    await settle()
    assert seen == [10]


async def test_watch_needs_no_reactive_service():
    """Hearing about a change must not cost the caller a second concept."""
    root = Context()
    await root.plugin(ConfigService, {"dict": {"http": {"timeout": 30}}})
    await settle()

    seen = []
    root.config.watch("http.timeout", lambda next_, prev: seen.append(next_))
    root.config.set("http.timeout", 60)
    await settle()
    assert seen == [60]


async def test_watch_dies_with_the_plugin_that_registered_it():
    root = await boot(dict={"http": {"timeout": 30}})
    seen = []

    def consumer(ctx, config=None):
        ctx.config.watch("http.timeout", lambda next_, prev: seen.append(next_))

    consumer.inject = ["config"]
    fiber = await root.plugin(consumer)
    await settle()

    root.config.set("http.timeout", 60)
    await settle()
    assert seen == [60]

    await fiber.dispose()
    await settle()
    root.config.set("http.timeout", 90)
    await settle()
    assert seen == [60], "a watcher outlived the plugin that registered it"


async def test_an_absent_key_reaches_the_callback_as_its_default():
    """The MISSING sentinel is an internal; a caller must never see it."""
    root = await boot(dict={})
    seen = []
    root.config.watch("http.timeout", lambda next_, prev: seen.append((next_, prev)), 30)

    root.config.set("http.timeout", 60)
    await settle()
    assert seen == [(60, 30)]


async def test_async_callbacks_are_serialized_per_watcher():
    """One watcher never runs twice at once — the reference chains a tail per watcher."""
    root = await boot(dict={"n": 0})
    running = 0
    overlaps = []
    order = []

    async def slow(next_, prev):
        nonlocal running
        running += 1
        if running > 1:
            overlaps.append(next_)
        await asyncio.sleep(0.01)
        order.append(next_)
        running -= 1

    root.config.watch("n", slow)
    root.config.set("n", 1)
    root.config.set("n", 2)
    root.config.set("n", 3)
    await asyncio.sleep(0.1)

    assert overlaps == [], f"a watcher was entered while already running: {overlaps}"
    assert order == [1, 2, 3], "serialized invocations lost their order"


async def test_a_raising_watcher_does_not_break_the_write():
    """A config write is not where an unrelated plugin's bug should surface."""
    root = await boot(dict={"n": 0})
    seen = []

    def boom(next_, prev):
        raise RuntimeError("watcher is broken")

    root.config.watch("n", boom)
    root.config.watch("n", lambda next_, prev: seen.append(next_))

    root.config.set("n", 1)
    await settle()
    assert seen == [1], "a raising watcher stopped the watchers after it"

    root.config.set("n", 2)
    await settle()
    assert seen == [1, 2], "a raising watcher stopped being called after failing"
