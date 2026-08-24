"""Extension points — spec 03.

One test per requirement. Each asserts the property the facility exists to
provide, not the shape of its API.
"""

from __future__ import annotations

import asyncio

import pytest

from plugkit import Context, Contribution, PointsService


async def settle(n: int = 15) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def a_root() -> Context:
    root = Context()
    await root.plugin(PointsService)
    return root


# ── R1: contribute without declaring, without knowing the reader ──────


async def test_a_point_needs_no_declaration():
    root = await a_root()
    root.points.add("http.routes", "handler")
    assert root.points.all("http.routes") == ["handler"]


async def test_contributors_do_not_know_each_other():
    root = await a_root()

    def alpha(ctx, config=None):
        ctx.points.add("http.routes", "a")

    def beta(ctx, config=None):
        ctx.points.add("http.routes", "b")

    alpha.inject = beta.inject = ["points"]
    await root.plugin(alpha)
    await root.plugin(beta)
    await settle()

    assert set(root.points.all("http.routes")) == {"a", "b"}


async def test_a_point_with_nothing_in_it_reads_empty():
    root = await a_root()
    assert root.points.all("nobody.contributed") == []
    assert root.points.get("nobody.contributed", "k") is None
    assert root.points.last("nobody.contributed") is None
    assert root.points.has("nobody.contributed") is False


# ── R2: the contribution dies with the contributor ────────────────────


async def test_unloading_the_contributor_removes_the_contribution():
    root = await a_root()

    def admin(ctx, config=None):
        ctx.points.add("http.routes", "admin-handler", key="/admin")

    admin.inject = ["points"]
    fiber = await root.plugin(admin)
    await settle()
    assert root.points.all("http.routes") == ["admin-handler"]

    await fiber.dispose()
    assert root.points.all("http.routes") == []


async def test_the_contributor_writes_no_teardown():
    """The plugin body returns nothing and still cleans up."""
    root = await a_root()

    def contributor(ctx, config=None):
        ctx.points.add("p", 1)
        ctx.points.add("p", 2)
        # no return, no disposer written by hand

    contributor.inject = ["points"]
    fiber = await root.plugin(contributor)
    await settle()
    assert root.points.count("p") == 2

    await fiber.dispose()
    assert root.points.count("p") == 0
    assert root.points.has("p") is False


async def test_the_disposer_can_be_called_early():
    root = await a_root()
    dispose = root.points.add("p", "v")
    assert root.points.all("p") == ["v"]
    dispose()
    assert root.points.all("p") == []


async def test_the_same_value_contributed_twice_removes_one_at_a_time():
    root = await a_root()
    value = object()
    first = root.points.add("p", value)
    root.points.add("p", value)
    assert root.points.count("p") == 2

    first()
    assert root.points.count("p") == 1


# ── R3: read them ─────────────────────────────────────────────────────


async def test_order_then_registration_order():
    root = await a_root()
    root.points.add("p", "second", order=10)
    root.points.add("p", "first", order=1)
    root.points.add("p", "third", order=10)
    assert root.points.all("p") == ["first", "second", "third"]


async def test_get_by_key():
    root = await a_root()
    root.points.add("p", "a", key="alpha")
    root.points.add("p", "b", key="beta")
    assert root.points.get("p", "beta") == "b"
    assert root.points.get("p", "absent") is None


async def test_get_resolves_by_arrival_not_by_order():
    """Two contributions, one key, different `order`. Arrival decides.

    `get` and `last` are both "the current one" for a key, so they must not
    disagree. Sorting by `order` first made `get` return whichever sorted last,
    which is the *first* arrival when the newer one carries a lower `order`.
    Every other test here uses the default `order=0`, where the two agree.
    """
    root = await a_root()
    root.points.add("p", "registered-first", key="k", order=10)
    root.points.add("p", "registered-second", key="k", order=1)

    assert root.points.get("p", "k") == "registered-second"
    assert root.points.get("p", "k") == root.points.last("p")


async def test_where_matches_on_properties():
    root = await a_root()
    root.points.add("p", "get-handler", method="GET")
    root.points.add("p", "post-handler", method="POST")
    assert root.points.where("p", method="GET") == ["get-handler"]


async def test_where_matches_inside_a_collection_property():
    root = await a_root()
    root.points.add("p", "both", methods=["GET", "POST"])
    root.points.add("p", "get-only", methods=["GET"])
    assert root.points.where("p", methods="POST") == ["both"]
    assert set(root.points.where("p", methods="GET")) == {"both", "get-only"}


async def test_where_ignores_contributions_without_the_property():
    root = await a_root()
    root.points.add("p", "tagged", method="GET")
    root.points.add("p", "untagged")
    assert root.points.where("p", method="GET") == ["tagged"]


async def test_last_is_the_most_recent():
    root = await a_root()
    root.points.add("p", "old")
    root.points.add("p", "new")
    assert root.points.last("p") == "new"


async def test_last_survives_out_of_order_disposal():
    """The property a 'current handler' slot needs.

    Save-and-restore is only correct when plugins unload in reverse mount
    order. Disposing the *older* registration must not resurrect its value.
    """
    root = await a_root()
    first = root.points.add("p", "old")
    root.points.add("p", "new")

    first()  # dispose the older one, out of order
    assert root.points.last("p") == "new"


async def test_names_lists_only_points_with_contributions():
    root = await a_root()
    dispose = root.points.add("alpha", 1)
    root.points.add("beta", 2)
    assert root.points.names() == ["alpha", "beta"]
    dispose()
    assert root.points.names() == ["beta"]


async def test_entries_carry_key_order_and_props():
    root = await a_root()
    root.points.add("p", "v", key="k", order=3, colour="red")
    (entry,) = root.points.entries("p")
    assert isinstance(entry, Contribution)
    assert (entry.value, entry.key, entry.order) == ("v", "k", 3)
    assert entry.props == {"colour": "red"}


def test_a_contribution_is_frozen():
    """Handing a consumer a mutable entry would let it edit another plugin's
    registration through the list it was given."""
    entry = Contribution(value="v", key="k")
    with pytest.raises(Exception):
        entry.value = "something else"


# ── R4: wake when the set changes ─────────────────────────────────────


async def test_on_change_fires_when_a_contribution_arrives():
    root = await a_root()
    seen = []
    root.points.on_change("p", lambda: seen.append(root.points.count("p")))

    root.points.add("p", 1)
    assert seen == [1]


async def test_on_change_fires_when_a_contribution_leaves():
    root = await a_root()
    dispose = root.points.add("p", 1)
    seen = []
    root.points.on_change("p", lambda: seen.append(root.points.count("p")))

    dispose()
    assert seen == [0]


async def test_on_change_does_not_fire_on_registration():
    root = await a_root()
    root.points.add("p", 1)
    seen = []
    root.points.on_change("p", lambda: seen.append("fired"))
    assert seen == []


async def test_on_change_is_per_point():
    root = await a_root()
    seen = []
    root.points.on_change("watched", lambda: seen.append(1))

    root.points.add("ignored", "x")
    assert seen == []
    root.points.add("watched", "y")
    assert seen == [1]


async def test_on_change_dies_with_the_consumer():
    root = await a_root()
    seen = []

    def consumer(ctx, config=None):
        ctx.points.on_change("p", lambda: seen.append(1))

    consumer.inject = ["points"]
    fiber = await root.plugin(consumer)
    await settle()

    root.points.add("p", "a")
    assert seen == [1]

    await fiber.dispose()
    root.points.add("p", "b")
    assert seen == [1], "the consumer unloaded and should no longer be called"


# ── R5: uniqueness ────────────────────────────────────────────────────


async def test_unique_rejects_a_duplicate_key():
    root = await a_root()
    root.points.add("tools", "first", key="bash", unique=True)
    with pytest.raises(ValueError, match="bash"):
        root.points.add("tools", "second", key="bash", unique=True)


async def test_unique_allows_the_key_again_after_the_first_is_disposed():
    root = await a_root()
    dispose = root.points.add("tools", "first", key="bash", unique=True)
    dispose()
    root.points.add("tools", "second", key="bash", unique=True)
    assert root.points.get("tools", "bash") == "second"


async def test_unique_without_a_key_is_rejected_at_the_call():
    root = await a_root()
    with pytest.raises(TypeError, match="needs a key"):
        root.points.add("p", "v", unique=True)


async def test_without_unique_a_key_may_repeat_and_the_last_wins():
    root = await a_root()
    root.points.add("p", "first", key="k")
    root.points.add("p", "second", key="k")
    assert root.points.get("p", "k") == "second"


# ── R6: isolation ─────────────────────────────────────────────────────


async def test_isolated_subtrees_hold_different_contributions():
    root = Context()
    left = root.isolate("points")
    right = root.isolate("points")
    await left.plugin(PointsService)
    await right.plugin(PointsService)
    await settle()

    left.points.add("p", "left-value")
    right.points.add("p", "right-value")

    assert left.points.all("p") == ["left-value"]
    assert right.points.all("p") == ["right-value"]


# ── argument validation ───────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", None, 42])
async def test_a_point_name_must_be_a_non_empty_string(bad):
    root = await a_root()
    with pytest.raises(TypeError):
        root.points.add(bad, "v")


async def test_a_key_must_be_a_string():
    root = await a_root()
    with pytest.raises(TypeError, match="key"):
        root.points.add("p", "v", key=42)


# ── the facility is domain-blind ──────────────────────────────────────


async def test_points_names_no_domain_concept():
    """The shelf test: it must not know what a tool, a route or a model is."""
    import inspect

    from plugkit.services import points

    source = inspect.getsource(points).lower()
    for word in ("tool", "route", "model", "agent", "llm", "prompt"):
        # allowed in prose, not in code
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        body = code.split('"""')
        executable = "".join(body[::2])  # drop docstrings
        assert word not in executable, f"points.py names the domain concept {word!r}"
