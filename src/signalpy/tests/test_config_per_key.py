"""Per-key reactive subscription tests for ConfigProvider.

Pre-change: every config.get(...) subscribed to one master `Signal[dict]`,
so any config.set(any_key, ...) re-ran every effect that had read any key.

Post-change: get(key) subscribes only to that key's lazy Signal; set(key, v)
notifies only that key's consumers (plus dotted descendants when a parent
is replaced). config.all() consumers still see every change via a separate
version Signal.

These tests would FAIL against the old single-master-Signal design.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from signalpy.kernel import Kernel
from signalpy.kernel.reactive import Effect
from signalpy.providers.config import ConfigProvider


@pytest.fixture
async def cfg():
    """Boot a minimal kernel containing only ConfigProvider, hand back the
    activated provider. Caller is responsible for nothing — kernel teardown
    is best-effort across tests since each test makes its own kernel."""
    kernel = Kernel()
    kernel.discover([ConfigProvider])
    # Seed defaults via the factory's instance properties
    kernel.instantiate("config", "config", {
        "defaults": {
            "server": {"port": 8080, "host": "localhost"},
            "db": {"url": "postgres://x"},
            "feature_flags": {"new_ui": False, "beta": True},
        }
    })
    await kernel.boot()
    provider = kernel.registry.require("IConfig")
    yield provider
    await kernel.shutdown()


# ── 1. Per-key isolation: write to A, B's reader does not re-run ───

@pytest.mark.asyncio
async def test_unrelated_key_write_does_not_retrigger_effect(cfg):
    """An @effect that reads only 'server.port' must NOT re-run when
    'db.url' changes. This is the core per-key win."""
    runs_port = {"n": 0}
    runs_db = {"n": 0}

    eff_port = Effect(lambda: (cfg.get("server.port"), runs_port.__setitem__("n", runs_port["n"] + 1)))
    eff_db = Effect(lambda: (cfg.get("db.url"), runs_db.__setitem__("n", runs_db["n"] + 1)))

    # Initial: each effect ran once at creation
    assert runs_port["n"] == 1
    assert runs_db["n"] == 1

    # Mutate db.url — port effect must NOT re-run
    cfg.set("db.url", "postgres://new")
    assert runs_port["n"] == 1, "port effect re-ran on unrelated db.url change"
    assert runs_db["n"] == 2

    # Mutate server.port — db effect must NOT re-run
    cfg.set("server.port", 9090)
    assert runs_port["n"] == 2
    assert runs_db["n"] == 2, "db effect re-ran on unrelated server.port change"

    eff_port.dispose()
    eff_db.dispose()


# ── 2. Same-key subscription still fires ───────────────────────────

@pytest.mark.asyncio
async def test_same_key_write_retriggers_effect(cfg):
    """Sanity: an effect reading key X DOES re-run when X changes."""
    seen = []
    eff = Effect(lambda: seen.append(cfg.get("server.port")))

    cfg.set("server.port", 9000)
    cfg.set("server.port", 9001)
    cfg.set("server.port", 9002)

    assert seen == [8080, 9000, 9001, 9002]
    eff.dispose()


# ── 3. Parent replace invalidates dotted descendants ───────────────

@pytest.mark.asyncio
async def test_parent_set_invalidates_descendant_readers(cfg):
    """If an effect read 'server.port' and someone replaces 'server' wholesale,
    the effect MUST still re-run — the value at 'server.port' has changed.
    This is the dotted-descendant fan-out the implementation does explicitly."""
    seen = []
    eff = Effect(lambda: seen.append(cfg.get("server.port")))

    assert seen == [8080]

    cfg.set("server", {"port": 9999, "host": "newhost"})
    assert seen == [8080, 9999], "descendant reader missed parent replacement"

    eff.dispose()


# ── 4. all() subscribers see every change ──────────────────────────

@pytest.mark.asyncio
async def test_all_consumer_sees_every_change(cfg):
    """An effect using config.all() subscribes to a version counter and
    must re-run on every set, regardless of which key changed."""
    runs = {"n": 0}
    eff = Effect(lambda: (cfg.all(), runs.__setitem__("n", runs["n"] + 1)))

    assert runs["n"] == 1
    cfg.set("server.port", 9000)
    assert runs["n"] == 2
    cfg.set("db.url", "postgres://other")
    assert runs["n"] == 3
    cfg.set("feature_flags.new_ui", True)
    assert runs["n"] == 4
    eff.dispose()


# ── 5. all() does NOT spuriously re-run unrelated per-key effects ──

@pytest.mark.asyncio
async def test_all_consumer_does_not_force_per_key_reruns(cfg):
    """A consumer using all() does not affect per-key consumers' behavior:
    a per-key effect still only fires for its own key."""
    runs_port = {"n": 0}
    runs_all = {"n": 0}

    eff_port = Effect(lambda: (cfg.get("server.port"), runs_port.__setitem__("n", runs_port["n"] + 1)))
    eff_all = Effect(lambda: (cfg.all(), runs_all.__setitem__("n", runs_all["n"] + 1)))

    assert runs_port["n"] == 1 and runs_all["n"] == 1

    cfg.set("db.url", "postgres://changed")
    assert runs_port["n"] == 1, "per-key effect re-ran for unrelated key"
    assert runs_all["n"] == 2, "all() consumer should re-run for any change"

    eff_port.dispose()
    eff_all.dispose()


# ── 6. Concurrent writes to different keys ─────────────────────────

@pytest.mark.asyncio
async def test_concurrent_writes_different_keys_no_race(cfg):
    """Threads writing to disjoint keys must not interfere. Each per-key
    Signal serializes its own writers; the lazy-create dict mutation is
    guarded by _key_lock."""
    N = 8
    PER = 100

    def writer(thread_idx: int):
        key = f"thread_{thread_idx}.value"
        for i in range(PER):
            cfg.set(key, i)

    with ThreadPoolExecutor(max_workers=N) as pool:
        list(pool.map(writer, range(N)))

    # Final value of each thread's key is PER - 1
    for t in range(N):
        assert cfg.get(f"thread_{t}.value") == PER - 1


# ── 7. Lazy creation: reading a never-set key still subscribes ─────

@pytest.mark.asyncio
async def test_get_then_later_set_notifies(cfg):
    """A consumer reads a key that doesn't exist yet (gets default).
    A later set on that key MUST notify the consumer — the lazy Signal
    is created on the get(), seeded with the (None) value."""
    seen = []
    # Read a key that's not in defaults — returns the default
    eff = Effect(lambda: seen.append(cfg.get("dynamic.flag", "off")))

    assert seen == ["off"]

    cfg.set("dynamic.flag", "on")
    assert seen == ["off", "on"], "consumer of nonexistent key was not notified on later set"
    eff.dispose()
