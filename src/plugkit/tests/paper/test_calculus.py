"""Tier 2 — the paper's Section 4 operational semantics, checked against the
real cordis runtime.

How to read a rule
==================

Section 4 of the paper specifies a plugin's life as ten *inference rules*.
A rule looks like

    O-Remove:   θn = Inactive(-)   ∀m. πm ≠ n
                -------------------------------
                γ  ⇒  γ ∖ n

Everything above the line is a *premise* ("this is when the rule applies"),
the part below is the *conclusion* ("this is what happens").  `n` is a
fiber — one running copy of a plugin — and `γ` is the whole system state.
Each test below builds the premises on purpose and asserts the conclusion.

The ten rules, in plain words
=============================

- **O-Insert** — the orchestrator asks for a plugin to exist.  It appears
  in a dormant state (`Inactive`), parked under its parent.
- **O-Retire** — the orchestrator asks for it to go away.  Retirement is a
  *request*: the lifecycle rules carry it out.
- **O-Remove** — a fully retired-and-unloaded fiber is dropped from the
  registry.  Its name may then be reused.
- **L-Begin** — a dormant fiber whose declared dependencies are all present
  starts *loading*: it runs its plugin body.
- **L-Iter** — a loading fiber that performs several changes step by step
  accumulates one undo button per step.
- **L-Finish** — the body completes; the fiber becomes *active* and its
  provisions become visible to others.
- **L-Divert** — while loading, the ground shifts under the fiber (a
  dependency changed); it must back out — applying the undo buttons
  collected so far — and never reach active.
- **L-Raise** — the body *fails*; the fiber still backs out cleanly and
  records the error (`Failed` = `Inactive(ξ)`), leaving nothing installed.
- **L-Leave** — an active fiber whose ground shifted is marked *unloading*
  immediately: it stops counting as a provider before anything is undone,
  so its dependents notice and start their own teardown first.
- **L-Unload** — an unloading fiber whose dependents are all gone applies
  its accumulated undo (the guard `¬relied`).

Two tests are marked `xfail(strict=True)`: they assert the *paper's* reading
of L-Begin and the L-Unload guard, which the shipped runtime (JS and this
port alike) does not implement — see README "Faithfulness notes".  If one of
them ever XPASSes, the runtime was aligned with the paper: update the note.
"""

from __future__ import annotations

import asyncio

import pytest

from plugkit.cordis import Context, FiberState


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def is_quiet(ctx) -> bool:
    """No fiber is mid-transition (the paper's `quiet`: every fiber has
    reached the state its target calls for)."""
    for runtime in list(ctx.registry.values()):
        for fiber in list(runtime.fibers):
            if fiber.inertia is not None:
                return False
    return True


async def settle(ctx, limit: int = 300) -> bool:
    """Let pending transitions run to completion (bounded, so a stuck test
    fails rather than hangs)."""
    for _ in range(limit):
        for _ in range(5):
            await asyncio.sleep(0)
        if is_quiet(ctx):
            return True
    return is_quiet(ctx)


async def fiber_settled(fiber, limit: int = 300) -> bool:
    """Like `settle`, but for one fiber that may already have been removed
    from the registry (a retired fiber is invisible to `is_quiet`)."""
    for _ in range(limit):
        for _ in range(5):
            await asyncio.sleep(0)
        if fiber.inertia is None:
            return True
    return fiber.inertia is None


async def tick(times: int = 20) -> None:
    """Advance the event loop a fixed number of steps (no quietness check)."""
    for _ in range(times):
        await asyncio.sleep(0)


def make_provider(key: str, value, gate: asyncio.Event = None):
    """A plugin providing `key`, optionally pausing mid-activation."""

    async def provider(ctx, config):
        ctx.provide(key, value)
        if gate is not None:
            await gate.wait()
        yield lambda: None

    provider.inject = []
    return provider


# ---------------------------------------------------------------------------
# orchestration rules — O-Insert, O-Retire, O-Remove
# ---------------------------------------------------------------------------


async def test_O_insert_creates_a_dormant_fiber():
    """O-Insert: instantiating a component adds a fiber with a fresh name,
    parked under its parent, in the dormant state — and nothing else."""
    ctx = Context()

    def consumer(ctx, config):
        raise AssertionError("must not run: its dependency is missing")

    consumer.inject = ["missing"]

    fiber = ctx.plugin(consumer)
    assert fiber.uid is not None  # n : 𝔑 — a fresh name
    assert fiber.parent.fiber is ctx.fiber  # π — parked under its parent
    assert ctx.registry.has(consumer)  # now in dom(F)
    assert fiber.state == FiberState.PENDING  # Inactive(⊥): no L-Begin yet

    other = ctx.plugin(lambda ctx, config: None)
    assert other.uid != fiber.uid  # names are never reused while live


async def test_O_insert_requires_disjoint_provisions():
    """O-Insert's last premise (`p ∩ p_m = ∅`): no two fibers may provide the
    same service — the "single source" discipline.

    The runtime has no static provision list, so it checks the premise when
    the second provider tries to *register* the key: the second fiber fails
    and the first is untouched.
    """
    ctx = Context()
    provider = make_provider("dep", 1)

    first = ctx.plugin(provider)
    assert await settle(ctx)
    assert first.state == FiberState.ACTIVE

    second = ctx.plugin(provider)
    assert await settle(ctx)
    assert second.state == FiberState.FAILED  # refused — key already owned
    assert ctx.get("dep") == 1  # the incumbent is unaffected


async def test_O_retire_is_synchronous_bookkeeping():
    """O-Retire: disposal marks the fiber retired (its name `n` is vacated)
    and removes it from the registry — before the asynchronous teardown has
    necessarily finished.

    (Retirement is separated from removal precisely so the accumulated undo
    is never discarded while still needed.)
    """
    ctx = Context()
    torn = []

    def plugin(ctx, config):
        return lambda: torn.append("disposed")

    fiber = ctx.plugin(plugin)
    assert await settle(ctx)
    assert fiber.state == FiberState.ACTIVE

    result = fiber.dispose()
    # retired the moment disposal begins — not a loop turn later
    assert fiber.uid is None
    assert fiber.state == FiberState.UNLOADING
    assert not ctx.registry.has(plugin)

    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
        await result
    assert await fiber_settled(fiber)
    assert fiber.state == FiberState.DISPOSED
    assert torn == ["disposed"]


async def test_O_remove_allows_reinsertion():
    """O-Remove and Theorem 59's discussion: once a fiber is fully unloaded
    it leaves the registry, and nothing stops the same plugin from being
    instantiated again under a fresh name."""
    ctx = Context()

    def plugin(ctx, config):
        return lambda: None

    first = ctx.plugin(plugin)
    assert await settle(ctx)
    first.dispose()
    assert await settle(ctx)
    assert ctx.registry.get(plugin) is None  # dropped from dom(F)

    second = ctx.plugin(plugin)
    assert await settle(ctx)
    assert second.state == FiberState.ACTIVE
    assert second is not first  # a fresh fiber, a fresh name


# ---------------------------------------------------------------------------
# activation rules — L-Begin, L-Iter, L-Finish, L-Divert, L-Raise
# ---------------------------------------------------------------------------


async def test_L_begin_waits_for_satisfaction():
    """L-Begin's premise (`target ≠ ⊥`): a fiber starts loading only once
    every declared dependency is actively provided; until then it sits
    dormant with no committed view — and it starts on its own the moment
    the specification becomes satisfied (Definition 26's *activating*
    classification).
    """
    ctx = Context()
    loaded = []

    def consumer(ctx, config):
        loaded.append(ctx.dep)  # reading through the committed view
        return lambda: loaded.append("unloaded")

    consumer.inject = ["dep"]

    c = ctx.plugin(consumer)
    assert c.state == FiberState.PENDING
    assert getattr(c, "store", None) is None  # ω exists only once installed

    p = ctx.plugin(make_provider("dep", 42))
    assert await settle(ctx)
    assert c.state == FiberState.ACTIVE
    assert loaded == [42]  # ran, and read the dependency it declared
    assert c.store["dep"].value == 42  # committed view ω recorded
    assert p.state == FiberState.ACTIVE


async def test_L_iter_accumulates_lifo():
    """L-Iter (and Algorithm 1 of §5.1.1): a multi-step activation hands the
    runtime one undo button per step; at teardown they are pressed in
    reverse order — last registered, first undone.
    """
    ctx = Context()
    order = []

    def plugin(ctx, config):
        ctx.effect(lambda: (lambda: order.append("effect-1")))
        yield lambda: order.append("yield-1")
        ctx.effect(lambda: (lambda: order.append("effect-2")))
        yield lambda: order.append("yield-2")

    fiber = ctx.plugin(plugin)
    assert await settle(ctx)
    assert fiber.state == FiberState.ACTIVE
    assert order == []  # nothing torn down while active

    fiber.dispose()
    assert await settle(ctx)
    assert order == ["yield-2", "effect-2", "yield-1", "effect-1"]


async def test_L_finish_publishes_only_when_active():
    """L-Finish plus the §4.3 visibility rule: a binding written *while the
    provider is still loading* is not yet visible to dependents — σ_γ
    counts only ACTIVE fibers.  The dependent activates when the provider
    finishes, not before.
    """
    ctx = Context()
    gate = asyncio.Event()
    observations = []

    def consumer(ctx, config):
        observations.append("ran")
        return lambda: None

    consumer.inject = ["dep"]
    c = ctx.plugin(consumer)
    assert c.state == FiberState.PENDING

    async def provider(ctx, config):
        ctx.provide("dep", 7)  # written — but the provider is only LOADING
        observations.append(c.state.name)
        await gate.wait()
        yield lambda: None

    p = ctx.plugin(provider)
    await tick()
    assert p.state == FiberState.LOADING
    assert observations == ["PENDING"]  # the dependent did NOT activate
    assert c.state == FiberState.PENDING

    gate.set()
    assert await settle(ctx)
    assert observations == ["PENDING", "ran"]  # ... only after L-Finish
    assert p.state == FiberState.ACTIVE


async def test_L_divert_backs_out_of_a_shifting_load():
    """L-Divert (with §4.3.3's *inertia*): if the ground shifts while a fiber
    is loading — here, its sole dependency is withdrawn mid-body — the
    fiber must back out: the step already in flight is allowed to land, no
    further step is started, everything collected so far is undone, and the
    fiber never reaches ACTIVE.
    """
    ctx = Context()
    events: list = []
    resumes: list = []
    status_log: list = []
    gate = asyncio.Event()

    async def consumer(ctx, config):
        resumes.append("r1")
        yield lambda: events.append("d1")
        resumes.append("r2")
        await gate.wait()  # the in-flight step hangs here ...
        yield lambda: events.append("d2")
        resumes.append("r3")  # ... so a third step must never be requested

    consumer.inject = ["dep"]

    # record every state change, for every fiber — filtered to `c` below
    ctx.on("internal/status", lambda fiber, old: status_log.append((fiber, fiber.state.name)))

    p = ctx.plugin(make_provider("dep", 1))
    assert await settle(ctx)

    c = ctx.plugin(consumer)
    await tick()
    assert c.state == FiberState.LOADING
    assert resumes == ["r1", "r2"]

    p.dispose()  # the dependency is withdrawn mid-transition
    await tick()
    gate.set()  # the in-flight step lands
    assert await settle(ctx)

    assert resumes == ["r1", "r2"]  # abort: no further iteration requested
    assert "r3" not in resumes
    assert events == ["d2", "d1"]  # landed, then fully recovered (LIFO)
    assert c.state == FiberState.PENDING  # target ⊥ → back to dormant
    # ... and it was never active — the transition routed through Unloading
    assert all(state != "ACTIVE" for fiber, state in status_log if fiber is c)


async def test_L_raise_recovers_and_records():
    """L-Raise: a failing activation is routed like every other teardown —
    the undo buttons collected before the failure are applied, nothing is
    left installed, and the error is recorded on the fiber (`Failed` is the
    paper's `Inactive(ξ)`, a dormant state that also carries the outcome).
    """
    ctx = Context()
    events: list = []

    def consumer(ctx, config):
        yield lambda: events.append("recovered")
        raise RuntimeError("boom")

    consumer.inject = ["dep"]
    p = ctx.plugin(make_provider("dep", 1))
    assert await settle(ctx)

    c = ctx.plugin(consumer)
    assert await settle(ctx)
    assert c.state == FiberState.FAILED
    assert events == ["recovered"]  # pre-failure effects were undone
    assert getattr(c, "store", None) is None  # carries no committed view

    with pytest.raises(RuntimeError, match="boom"):
        await c.await_()  # awaiting a failed fiber re-raises its error

    assert ctx.registry.has(consumer)  # ... but it obstructs nothing else


# ---------------------------------------------------------------------------
# deactivation rules — L-Leave, L-Unload (and the guard)
# ---------------------------------------------------------------------------


async def test_L_leave_stops_providing_before_undoing():
    """L-Leave: the moment a provider's teardown begins it is marked
    *unloading* — it stops counting as a provider immediately, so its
    dependents recompute an unsatisfied target and start their own teardown
    while every binding is still in place.
    """
    ctx = Context()
    reads: list = []

    def consumer(ctx, config):
        async def teardown():
            # read through the dependent's OWN context: the committed view
            # (ω) is what stays readable throughout its own teardown
            reads.append(ctx.dep)

        return teardown

    consumer.inject = ["dep"]

    p = ctx.plugin(make_provider("dep", 42))
    c = ctx.plugin(consumer)
    assert await settle(ctx)
    assert c.state == FiberState.ACTIVE

    p.dispose()
    assert p.state == FiberState.UNLOADING  # out of service, synchronously
    assert p.uid is None

    assert await settle(ctx)
    assert await fiber_settled(p)
    assert reads == [42]  # Theorem 63's readability half, observed live
    assert c.state == FiberState.PENDING  # the dependent went dormant too
    assert ctx.get("dep") is None  # and the binding is finally gone


async def test_L_unload_guard_holds_the_binding():
    """The L-Unload guard (`¬relied`), runtime form: a provider's withdrawal
    waits for its notified dependents to finish before releasing the
    binding — the dependent observes the binding held for its entire
    teardown, and only then is it cleared.
    """
    ctx = Context()
    held: list = []
    order: list = []
    provider_fiber = None

    def consumer(ctx, config):
        async def teardown():
            order.append("dependent-start")
            # the provider fiber's committed store must still hold the
            # binding for as long as the dependent is tearing down
            held.append("dep" in (provider_fiber.store or {}))
            order.append("dependent-end")

        return teardown

    consumer.inject = ["dep"]

    p = ctx.plugin(make_provider("dep", 42))
    provider_fiber = p
    c = ctx.plugin(consumer)
    assert await settle(ctx)

    p.dispose()
    assert await settle(ctx)
    assert await fiber_settled(p)
    assert order == ["dependent-start", "dependent-end"]
    assert held == [True]  # binding held throughout the dependent's teardown
    assert provider_fiber.store is None  # released only afterwards


@pytest.mark.xfail(
    strict=True,
    reason="paper §5.1.3 places the L-Unload wait ahead of the whole recovery; "
    "the runtime (JS and this port) places it inside the provide disposer, so "
    "a provider's OTHER effects tear down concurrently — see README "
    "'Faithfulness notes'",
)
async def test_L_unload_guard_orders_the_whole_recovery():
    """Theorem 63's stronger half: a provider's *entire* teardown — not just
    the binding — should wait until its dependents have finished theirs.
    (Expected to fail: the documented inherited deviation.)"""
    ctx = Context()
    order: list = []
    provider_fiber = None

    async def provider_teardown():
        order.append("provider-other-start")
        await asyncio.sleep(0.01)
        order.append("provider-other-end")

    async def provider(ctx, config):
        ctx.provide("dep", 42)
        ctx.effect(lambda: provider_teardown)

    async def teardown():
        order.append("dependent-start")
        await asyncio.sleep(0.05)
        order.append("dependent-end")

    def consumer(ctx, config):
        return teardown

    consumer.inject = ["dep"]

    p = ctx.plugin(provider)
    provider_fiber = p
    c = ctx.plugin(consumer)
    assert await settle(ctx)

    p.dispose()
    assert await settle(ctx)
    assert order.index("dependent-end") < order.index("provider-other-end")


@pytest.mark.xfail(
    strict=True,
    reason="paper §4.3.4 withholds failed fibers (L-Begin requires "
    "Inactive(⊥)); the runtime re-enters them when the target digest "
    "changes — see README 'Faithfulness notes'",
)
async def test_L_begin_ignores_failed_fibers():
    """L-Begin's premise read strictly: a fiber that failed should not be
    retried when its dependency is later re-provided by a *different*
    fiber.  (Expected to fail: the documented inherited deviation.)"""
    ctx = Context()
    loads: list = []

    def consumer(ctx, config):
        loads.append(1)
        if len(loads) == 1:
            raise RuntimeError("first load fails")
        return lambda: None

    consumer.inject = ["dep"]

    p1 = ctx.plugin(make_provider("dep", 1))
    c = ctx.plugin(consumer)
    assert await settle(ctx)
    assert c.state == FiberState.FAILED
    assert loads == [1]

    p1.dispose()
    assert await settle(ctx)
    p2 = ctx.plugin(make_provider("dep", 2))
    assert await settle(ctx)

    assert loads == [1]  # the paper says: still withheld


# ---------------------------------------------------------------------------
# Definition 47 — instantiation as a tracked effect
# ---------------------------------------------------------------------------


async def test_definition47_registration_cascades():
    """Definition 47 (§4.2): a plugin instantiated *by* another plugin is an
    ordinary tracked effect of the parent — so retiring the parent retires
    the child (the registration's inverse is an O-Retire), cascading
    teardown down the tree without any special case.
    """
    ctx = Context()
    children: list = []
    torn: list = []

    def child(ctx, config):
        ctx.effect(lambda: (lambda: torn.append("child-effect")), "child-effect")

    def parent(ctx, config):
        children.append(ctx.plugin(child))

    f = ctx.plugin(parent)
    assert await settle(ctx)
    assert children[0].state == FiberState.ACTIVE

    f.dispose()
    assert await settle(ctx)
    assert await fiber_settled(children[0])
    assert children[0].uid is None  # the child was retired with its parent
    assert children[0].state == FiberState.DISPOSED
    assert torn == ["child-effect"]


# ---------------------------------------------------------------------------
# Table 2 — the theory-to-implementation dictionary
# ---------------------------------------------------------------------------


async def test_table2_target_is_a_digest_of_provider_identity():
    """Table 2 / §5.1.3: a fiber's target is a digest of *which fibers*
    provide its declared keys — identity, not value.

    Consequences checked here: overwriting a binding in place is not
    observed (no reload), while re-providing an equal value through a NEW
    fiber is observed (reload) — a uid is never mistaken for the fiber it
    replaced.
    """
    ctx = Context()
    loads: list = []

    async def consumer(ctx, config):
        loads.append(1)
        ctx.on("ping", lambda: None)
        yield lambda: None

    consumer.inject = ["dep"]

    async def provider(ctx, config):
        ctx.provide("dep", 42)
        # the owning fiber may rewrite its own binding in place; doing so
        # through a listener keeps the write inside the provider's fiber
        ctx.on("refresh", lambda value: ctx.set("dep", value))
        yield lambda: None

    p = ctx.plugin(provider)
    c = ctx.plugin(consumer)
    assert await settle(ctx)
    assert len(loads) == 1
    # white-box: the epoch digest is the tuple of provider uids (§5.1.3)
    assert c._runner.epoch == f":{p.uid}"

    # in-place value change: same provider fiber → no reload ...
    ctx.emit("refresh", 43)
    assert await settle(ctx)
    assert len(loads) == 1
    assert ctx.get("dep") == 43

    # ... but a NEW provider fiber, even with an equal value, reloads
    p.dispose()
    assert await settle(ctx)
    p2 = ctx.plugin(make_provider("dep", 43))  # equal value, different fiber
    assert await settle(ctx)
    assert len(loads) == 2
    assert c._runner.epoch == f":{p2.uid}"
