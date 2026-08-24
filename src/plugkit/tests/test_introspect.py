"""The system describes itself — spec 04."""

from __future__ import annotations

import asyncio
import json

from plugkit import (
    DIAGNOSTICS,
    Context,
    PointsService,
    describe,
    format_tree,
    provide,
)


async def settle(n: int = 15) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class Database:
    def __init__(self):
        self.pool_size = 4


class Greeter:
    def __init__(self, database):
        self.database = database


def by_name(snapshot: dict, name: str) -> dict:
    return next(f for f in snapshot["fibers"] if f["name"] == name)


# ── R1: works on a system that never planned for it ───────────────────


async def test_describe_needs_nothing_mounted():
    root = Context()
    snapshot = describe(root)
    assert snapshot["fibers"] == []
    assert snapshot["services"] == {}


async def test_describe_takes_any_context_with_no_setup():
    """No plugin mounted, no configuration, no prior registration."""
    root = Context()
    await root.plugin(provide(Database, "database"))
    await settle()

    assert describe(root)["services"] == {"database": by_name(describe(root), "database")["uid"]}


# ── R2: plain data ────────────────────────────────────────────────────


async def test_a_snapshot_survives_json_dumps():
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(provide(Database, "database"))
    await settle()

    text = json.dumps(describe(root))
    assert json.loads(text)["services"]["database"]


async def test_a_snapshot_holds_uids_not_fibers():
    """Holding a snapshot must not keep the system alive."""
    root = Context()
    await root.plugin(provide(Database, "database"))
    await settle()

    entry = by_name(describe(root), "database")
    assert isinstance(entry["uid"], int)
    assert entry["parent"] is None or isinstance(entry["parent"], int)


# ── R3: per-fiber facts ───────────────────────────────────────────────


async def test_it_reports_state_provides_and_injects():
    root = Context()
    await root.plugin(provide(Database, "database"))
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    snapshot = describe(root)
    greeter = by_name(snapshot, "greeter")
    assert greeter["state"] == "ACTIVE"
    assert greeter["provides"] == ["greeter"]
    assert greeter["injects"] == ["database"]


async def test_it_reports_the_effects_a_plugin_registered():
    root = Context()

    def watcher(ctx, config=None):
        ctx.on("something", lambda: None)

    await root.plugin(watcher)
    await settle()

    effects = by_name(describe(root), "watcher")["effects"]
    assert any("something" in label for label in effects), effects


async def test_it_reports_a_failure_with_its_error():
    root = Context()

    def broken(ctx, config=None):
        raise ConnectionRefusedError("nothing listening")

    fiber = root.plugin(broken)
    try:
        await fiber
    except ConnectionRefusedError:
        pass

    entry = by_name(describe(root), "broken")
    assert entry["state"] == "FAILED"
    assert "ConnectionRefusedError" in entry["error"]
    assert "nothing listening" in entry["error"]


# ── R4: who provides what ─────────────────────────────────────────────


async def test_services_map_to_the_providing_fiber():
    root = Context()
    await root.plugin(provide(Database, "database"))
    await settle()

    snapshot = describe(root)
    assert snapshot["services"]["database"] == by_name(snapshot, "database")["uid"]


# ── R5: the missing-service report ────────────────────────────────────


async def test_a_pending_fiber_names_what_it_is_waiting_for():
    """The single most useful fact in the snapshot."""
    root = Context()
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    entry = by_name(describe(root), "greeter")
    assert entry["state"] == "PENDING"
    assert entry["missing"] == ["database"]


async def test_missing_lists_only_the_absent_ones():
    root = Context()

    def needs_two(ctx, config=None):
        pass

    needs_two.inject = ["database", "cache"]
    await root.plugin(provide(Database, "database"))
    await root.plugin(needs_two)
    await settle()

    entry = by_name(describe(root), "needs_two")
    assert entry["injects"] == ["cache", "database"]
    assert entry["missing"] == ["cache"]


async def test_missing_empties_once_the_service_arrives():
    root = Context()
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()
    assert by_name(describe(root), "greeter")["missing"] == ["database"]

    await root.plugin(provide(Database, "database"))
    await settle()
    assert by_name(describe(root), "greeter")["missing"] == []


# ── R6: diagnostics ───────────────────────────────────────────────────


async def test_a_plugin_can_contribute_a_diagnostic():
    root = Context()
    await root.plugin(PointsService)

    def database(ctx, config=None):
        pool = Database()
        ctx.provide("database", pool)
        ctx.points.add(DIAGNOSTICS, lambda: {"pool_size": pool.pool_size}, key="database")

    database.inject = ["points"]
    await root.plugin(database)
    await settle()

    assert describe(root)["diagnostics"]["database"] == {"pool_size": 4}


async def test_a_diagnostic_that_raises_is_reported_not_propagated():
    """A broken diagnostic must not break the tool you reached for."""
    root = Context()
    await root.plugin(PointsService)

    def bad(ctx, config=None):
        def boom():
            raise RuntimeError("the metric is broken")

        ctx.points.add("diagnostics", boom, key="bad")

    bad.inject = ["points"]
    await root.plugin(bad)
    await settle()

    reported = describe(root)["diagnostics"]["bad"]
    assert "RuntimeError" in reported["error"]
    assert "the metric is broken" in reported["error"]


async def test_a_diagnostic_dies_with_its_plugin():
    root = Context()
    await root.plugin(PointsService)

    def temporary(ctx, config=None):
        ctx.points.add("diagnostics", lambda: {"up": True}, key="temporary")

    temporary.inject = ["points"]
    fiber = await root.plugin(temporary)
    await settle()
    assert "temporary" in describe(root)["diagnostics"]

    await fiber.dispose()
    await settle()
    assert "temporary" not in describe(root)["diagnostics"]


async def test_diagnostics_are_empty_without_points_mounted():
    root = Context()
    await root.plugin(provide(Database, "database"))
    await settle()
    assert describe(root)["diagnostics"] == {}


async def test_points_are_summarised_when_mounted():
    root = Context()
    await root.plugin(PointsService)
    root.points.add("http.routes", "a")
    root.points.add("http.routes", "b")

    assert describe(root)["points"]["http.routes"] == 2


# ── R7: the tree ──────────────────────────────────────────────────────


async def test_the_tree_names_every_fiber_and_its_state():
    root = Context()
    await root.plugin(provide(Database, "database"))
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    text = format_tree(describe(root))
    assert "database" in text
    assert "greeter" in text
    assert "ACTIVE" in text
    assert "2 fibers" in text


async def test_the_tree_shows_what_a_pending_fiber_waits_for():
    root = Context()
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    assert "waiting for: database" in format_tree(describe(root))


async def test_the_tree_shows_a_failure():
    root = Context()

    def broken(ctx, config=None):
        raise ValueError("bad config")

    fiber = root.plugin(broken)
    try:
        await fiber
    except ValueError:
        pass

    text = format_tree(describe(root))
    assert "FAILED" in text
    assert "bad config" in text


async def test_the_tree_renders_from_a_stored_snapshot():
    """It takes a snapshot, not a context, so a logged one renders the same."""
    root = Context()
    await root.plugin(provide(Database, "database"))
    await settle()

    live = describe(root)
    stored = json.loads(json.dumps(live))
    assert format_tree(stored) == format_tree(live)
    assert "database" in format_tree(stored)


async def test_the_tree_of_an_empty_system_does_not_crash():
    assert "0 fibers" in format_tree(describe(Context()))


# ── read-only ─────────────────────────────────────────────────────────


async def test_describe_changes_nothing():
    root = Context()
    await root.plugin(provide(Database, "database"))
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    before = describe(root)
    for _ in range(5):
        describe(root)
    after = describe(root)

    del before["taken_at"], after["taken_at"]
    assert before == after


def test_the_diagnostics_point_name_is_exported():
    """A plugin contributes to this point without importing `describe`."""
    assert DIAGNOSTICS == "diagnostics"


async def test_a_diagnostic_returning_a_non_serialisable_object_keeps_r2():
    """R2 is a promise to every reader of a snapshot. One plugin returning a
    socket, a datetime or a set must not break it for everyone."""
    root = Context()
    await root.plugin(PointsService)

    class Connection:
        def __repr__(self):
            return "<Connection open>"

    def odd(ctx, config=None):
        ctx.points.add(DIAGNOSTICS, lambda: {"conn": Connection()}, key="odd")

    odd.inject = ["points"]
    await root.plugin(odd)
    await settle()

    snapshot = describe(root)
    json.dumps(snapshot)                       # the promise R2 makes
    assert "<Connection open>" in str(snapshot["diagnostics"]["odd"])
