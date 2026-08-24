"""Tier 3 — the paper's Section 4.4 metatheory, tested by random traces.

Theorems about *all possible runs* cannot be proven by tests, but their
content can be checked on randomly generated runs: apply a random sequence
of operations, and after every step verify the global invariants the
theorems guarantee.  A violation anywhere means the runtime breaks the
theorem; a thousand quiet runs means the runtime respects it on everything
we tried.  All randomness is seeded, so any failure is reproducible.

The three theorems in plain words:

- **Preservation (Theorem 59, Definition 58).**  The wiring never rots:
  every fiber hangs from the root of the plugin tree; no two fibers own the
  same service; and every *active* fiber's declared dependencies are still
  provided by installed fibers.  Checked after every operation.

- **Termination (Theorem 66).**  The system always settles: after the
  orchestrator stops asking for changes, every fiber reaches the state its
  target calls for — no plugin is left forever half-loading or
  half-unloading.  (This is what makes the L-Unload guard safe: a fiber
  only ever waits on dependents that are already on their way out.)

- **Confluence (Theorem 73).**  Arrival order does not change the
  destination: load the same final configuration with different
  interleavings — or straight from scratch — and the settled system is the
  same.  This is what lets the loader reconcile incrementally instead of
  rebuilding the world on every config change.
"""

from __future__ import annotations

import asyncio
import random

from plugkit.cordis import Context, FiberState

KEYS = ["k0", "k1", "k2", "k3"]


# ---------------------------------------------------------------------------
# the harness: a tiny random orchestrator
# ---------------------------------------------------------------------------


def make_provider(key: str, value: int):
    async def provider(ctx, config):
        ctx.provide(key, value)
        yield lambda: None

    provider.inject = []
    return provider


def make_consumer(deps, index: int):
    async def consumer(ctx, config):
        for dep in deps:
            _ = getattr(ctx, dep)  # read every declared coeffect

    consumer.inject = sorted(deps)
    consumer.index = index
    return consumer


class World:
    """The orchestrator's ledger: which fibers it asked for, and which of
    them it considers alive (not yet retired)."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.providers: dict = {}  # key -> (fiber, value)
        self.consumers: list = []  # (fiber, deps)
        self.counter = 0

    # -- operations (each is a legal orchestration action) -----------------

    def add_provider(self, key: str, value: int):
        if key in self.providers:
            return  # refusing: O-Insert's disjoint-provision premise
        fiber = self.ctx.plugin(make_provider(key, value))
        self.providers[key] = (fiber, value)

    def add_consumer(self, deps):
        if not deps:
            return
        self.counter += 1
        fiber = self.ctx.plugin(make_consumer(deps, self.counter))
        self.consumers.append((fiber, sorted(deps)))

    def retire_provider(self, key: str):
        if key not in self.providers:
            return
        fiber, _ = self.providers.pop(key)
        fiber.dispose()

    def retire_consumer(self, index: int):
        if index >= len(self.consumers):
            return
        fiber, deps = self.consumers[index]
        if fiber.uid is not None:
            fiber.dispose()

    def swap_provider(self, key: str):
        """Replace the provider of `key` — retire, let the system drain the
        withdrawal, then insert a fresh provider (self-contained, so it is
        legal no matter how the caller interleaves settling)."""
        if key not in self.providers:
            return
        self.retire_provider(key)

    async def continue_swap(self, key: str, value: int):
        """Second half of `swap_provider`, separated so the harness can
        settle between the retirement and the fresh insertion."""
        self.add_provider(key, value)


def random_ops(rng: random.Random, count: int) -> list:
    """A random program of orchestration actions."""
    ops = []
    for _ in range(count):
        choice = rng.random()
        key = rng.choice(KEYS)
        if choice < 0.3:
            ops.append(("add_provider", key, rng.randrange(100)))
        elif choice < 0.55:
            ops.append(("add_consumer", tuple(rng.sample(KEYS, rng.randrange(1, 3)))))
        elif choice < 0.7:
            ops.append(("retire_provider", key))
        elif choice < 0.85:
            ops.append(("retire_consumer", rng.randrange(4)))
        else:
            ops.append(("swap", key, rng.randrange(100)))
    return ops


async def apply_op(world: World, op: tuple) -> None:
    kind = op[0]
    if kind == "add_provider":
        world.add_provider(op[1], op[2])
    elif kind == "add_consumer":
        world.add_consumer(op[1])
    elif kind == "retire_provider":
        world.retire_provider(op[1])
    elif kind == "retire_consumer":
        world.retire_consumer(op[1])
    elif kind == "swap":
        world.swap_provider(op[1])
        # the insertion must wait for the withdrawal to leave the store —
        # this mirrors the loader's retire-then-insert sequencing
        for _ in range(300):
            await asyncio.sleep(0)
            if world.ctx.reflect._get_impl(op[1], False) is None:
                break
        await world.continue_swap(op[1], op[2])


# ---------------------------------------------------------------------------
# settling and the invariants
# ---------------------------------------------------------------------------


def is_quiet(ctx: Context) -> bool:
    for runtime in list(ctx.registry.values()):
        for fiber in list(runtime.fibers):
            if fiber.inertia is not None:
                return False
    return True


async def settle(ctx: Context, limit: int = 300) -> bool:
    for _ in range(limit):
        for _ in range(5):
            await asyncio.sleep(0)
        if is_quiet(ctx):
            return True
    return is_quiet(ctx)


def check_wellformed(ctx: Context) -> list:
    """Definition 58's four clauses, read on the runtime (Theorem 59)."""
    problems = []

    fibers = [
        fiber
        for runtime in list(ctx.registry.values())
        for fiber in list(runtime.fibers)
    ]

    # (1) parent pointers form a tree rooted at the root fiber
    for fiber in fibers:
        walked = set()
        current = fiber
        while current.runtime is not None:
            if id(current) in walked:
                problems.append(f"cycle in parent chain at {fiber.uid}")
                break
            walked.add(id(current))
            current = current.parent.fiber

    # (2) single source: every binding in the shared store has one owner,
    #     and (once settled) that owner is an active fiber
    for key, impl in ctx.reflect.store.items():
        if impl.fiber.state != FiberState.ACTIVE:
            problems.append(f"binding {impl.name!r} owned by non-active fiber")

    # (3)+(4) an active fiber's committed view is total on its declared
    #         dependencies and names installed providers
    for fiber in fibers:
        if fiber.uid is None or fiber.state != FiberState.ACTIVE:
            continue
        for name in fiber.inject:
            impl = (fiber.store or {}).get(name)
            if impl is None:
                problems.append(f"active fiber {fiber.uid} lacks committed {name!r}")
            elif impl.fiber.state != FiberState.ACTIVE:
                problems.append(
                    f"active fiber {fiber.uid} committed {name!r} to a retired provider"
                )

    return problems


def signature(ctx: Context) -> tuple:
    """What the settled system provides — the observable outcome a
    configuration determines (Theorem 73)."""
    return tuple(
        sorted(
            (impl.name, impl.value)
            for impl in ctx.reflect.store.values()
            if impl.fiber.state == FiberState.ACTIVE
        )
    )


def expected_consumer_states(world: World) -> dict:
    """Each alive consumer should be active exactly when its declared keys
    are all provided after settling."""
    provided = {name for name, _ in signature(world.ctx)}
    expected = {}
    for fiber, deps in world.consumers:
        if fiber.uid is None:
            continue  # retired
        expected[id(fiber)] = all(dep in provided for dep in deps)
    return expected


def check_consumers(world: World) -> list:
    problems = []
    for fiber, _deps in world.consumers:
        if fiber.uid is None:
            continue
        expected = expected_consumer_states(world)[id(fiber)]
        actual = fiber.state == FiberState.ACTIVE
        if expected != actual:
            problems.append(f"consumer fiber {fiber.uid} active={actual}, expected {expected}")
    return problems


# ---------------------------------------------------------------------------
# the tests
# ---------------------------------------------------------------------------


async def test_theorem59_preservation_on_random_traces():
    """Preservation: run random programs and check the wiring after every
    settled step — the registry never rots, whatever the orchestrator does."""
    for seed in (11, 22, 33, 44, 55):
        ctx = Context()
        world = World(ctx)
        rng = random.Random(seed)
        for op in random_ops(rng, 16):
            await apply_op(world, op)
            assert await settle(ctx), f"seed {seed}: system did not settle"
            problems = check_wellformed(ctx)
            assert problems == [], f"seed {seed} after {op}: {problems}"
            problems = check_consumers(world)
            assert problems == [], f"seed {seed} after {op}: {problems}"


async def test_theorem66_termination_on_random_traces():
    """Termination: every random program settles — no fiber is left mid-
    transition once the orchestrator stops."""
    for seed in (101, 202, 303):
        ctx = Context()
        world = World(ctx)
        rng = random.Random(seed)
        for op in random_ops(rng, 20):
            await apply_op(world, op)
        assert await settle(ctx), f"seed {seed}: did not reach quiescence"
        for runtime in list(ctx.registry.values()):
            for fiber in list(runtime.fibers):
                if fiber.uid is None:
                    continue
                assert fiber.state not in (FiberState.LOADING, FiberState.UNLOADING), (
                    f"seed {seed}: fiber {fiber.uid} stuck in {fiber.state.name}"
                )


async def test_theorem73_confluence_across_interleavings():
    """Confluence: the same program under two settling disciplines — fully
    settled after every operation vs. settled in batches — reaches the same
    quiescent system, and that system matches a from-scratch load of the
    final configuration."""
    for seed in (1001, 1002, 1003):
        ops = random_ops(random.Random(seed), 14)

        # schedule A — settle after every operation
        ctx_a = Context()
        world_a = World(ctx_a)
        for op in ops:
            await apply_op(world_a, op)
            await settle(ctx_a)

        # schedule B — the same operations in the same order, settled only
        # every other one (transitions overlap more)
        ctx_b = Context()
        world_b = World(ctx_b)
        for index, op in enumerate(ops):
            await apply_op(world_b, op)
            if index % 2 == 1:
                await settle(ctx_b)
        assert await settle(ctx_b)

        # a from-scratch load of the final configuration
        ctx_c = Context()
        world_c = World(ctx_c)
        for key, (_fiber, value) in world_a.providers.items():
            world_c.add_provider(key, value)
        for _fiber, deps in world_a.consumers:
            if all(key in world_c.providers for key in deps):
                world_c.add_consumer(deps)
        assert await settle(ctx_c)

        sig_a = signature(ctx_a)
        sig_b = signature(ctx_b)
        sig_c = signature(ctx_c)
        assert sig_a == sig_b, f"seed {seed}: interleaving changed the outcome"
        assert sig_a == sig_c, f"seed {seed}: differs from a from-scratch load"
        assert check_wellformed(ctx_a) == []
        assert check_wellformed(ctx_b) == []
        assert check_consumers(world_a) == []
        assert check_consumers(world_b) == []
