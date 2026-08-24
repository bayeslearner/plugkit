"""Tier 1 — the paper's Section 3 algebra, tested against the reference model.

Each test below corresponds to a numbered statement in the paper ("A
Programming Paradigm for Spatiotemporal Composability").  The statements are
equalities between *functions*, so a test checks them *pointwise*: evaluate
both sides at (ideally) every possible input and compare.  On the finite
world with `n = 2` there are so few cases that the check is exhaustive — a
complete verification for that model.  On `n = 3` we sample inputs with a
seeded random generator (reproducible: the seed is printed in the test id).

Plain-language glosses are given per test; the full vocabulary tour lives in
`tests/paper/reference.py`.
"""

from __future__ import annotations

import random

from . import reference as ref

N = 3  # size of the finite world used for sampled checks


def exhaustive_pairs(n: int):
    fns = ref.all_functions(n)
    return [(f, g) for f in fns for g in fns]


# ---------------------------------------------------------------------------
# track / recover — Definitions 3 and 6, Theorems 4, 5, 7
# ---------------------------------------------------------------------------


def test_theorem_4_track_commutes_with_projection():
    """Theorem 4 (§3.1.1): tracking a change and then looking at the world is
    the same as looking at the world and then applying the change.

    In plain words: `track` really does apply `f` to the world — the
    bookkeeping it adds never distorts the change itself.
    """
    for f, g in exhaustive_pairs(2):  # every (change, undo) pair
        for gamma in range(2):
            state = (gamma, ref.identity_fn(2))
            # "look at the world" = take the first component
            assert ref.track(f, g)(state)[0] == f[gamma]


def test_theorem_5_track_is_a_monoid_homomorphism():
    """Theorem 5 (§3.1.1): composing two (change, undo) pairs and then
    tracking them equals tracking them one after the other.

    Plain words: it makes no difference whether you bundle changes together
    first and record one combined undo, or record each undo as you go — the
    accumulator ends up identical.  This is why a component's whole teardown
    can be derived from its loading, step by step.
    """
    ident = ref.identity_fn(2)

    # clause 1 — the unit: tracking the do-nothing pair does nothing at all
    for state in ref.all_partial_states(2):
        assert ref.track(ident, ident)(state) == state

    # clause 2 — the multiplication: track(p1 ∘ p2) == track(p1) ∘ track(p2)
    for f1, g1 in exhaustive_pairs(2):
        for f2, g2 in exhaustive_pairs(2):
            pair_composite = ref.compose_pair((f1, g1), (f2, g2))
            for state in ref.all_partial_states(2):
                left = ref.track(*pair_composite)(state)
                right = ref.track(f1, g1)(ref.track(f2, g2)(state))
                assert left == right


def test_theorem_7_soundness_invariant():
    """Theorem 7 (§3.1.1) — the *soundness invariant*, the heart of temporal
    composability.

    Plain words: if every undo button really undoes its own change, then
    pressing the accumulated undo at ANY later point returns the world to
    where it was before that change — earlier undos on the chain are not
    disturbed.  "Unplug the component" therefore reliably restores the
    pre-component world.
    """
    rng = random.Random(101)
    n = N
    trials = 0
    for f, g in exhaustive_pairs(2):
        for gamma in range(2):
            if g[f[gamma]] != gamma:
                continue  # hypothesis not met: this undo does not undo f here
            for phi in ref.all_functions(2):
                state = (gamma, phi)
                assert ref.recover(ref.track(f, g)(state)) == ref.recover(state)
                trials += 1
    assert trials > 0

    # sampled version on the larger world, with a witness checked per state
    for _ in range(300):
        f = ref.random_function(rng, n)
        g = ref.random_function(rng, n)
        gamma = rng.randrange(n)
        if g[f[gamma]] != gamma:
            continue
        phi = ref.random_function(rng, n)
        state = (gamma, phi)
        assert ref.recover(ref.track(f, g)(state)) == ref.recover(state)


# ---------------------------------------------------------------------------
# Effect functions and ⋄ — Definitions 8 and 9, Theorems 10, 11
# ---------------------------------------------------------------------------


def test_theorem_10_diamond_is_a_monoid():
    """Theorem 10 (§3.1.2): effect composition `⋄` ("run the second, then the
    first; undo in reverse") is associative with a unit.

    Plain words: however you bracket a sequence of bundled changes, the
    combined change — and the combined undo — comes out the same, and doing
    nothing is a valid element.  This is what lets the runtime fold many
    undo buttons into one without the bracketing mattering.
    """
    rng = random.Random(202)
    n = N
    unit = ref.effect_unit(n)
    worlds = range(n)

    for _ in range(150):
        e1 = ref.random_effect(rng, n, witnessed=False)  # laws need no witness
        e2 = ref.random_effect(rng, n, witnessed=False)
        e3 = ref.random_effect(rng, n, witnessed=False)

        # unit laws: doing nothing before or after changes nothing
        for gamma in worlds:
            assert ref.effect_compose(e1, unit)[gamma] == e1[gamma]
            assert ref.effect_compose(unit, e1)[gamma] == e1[gamma]

        # associativity: (e1 ⋄ e2) ⋄ e3 == e1 ⋄ (e2 ⋄ e3), pointwise
        left = ref.effect_compose(ref.effect_compose(e1, e2), e3)
        right = ref.effect_compose(e1, ref.effect_compose(e2, e3))
        for gamma in worlds:
            assert left[gamma] == right[gamma]


def test_theorem_11_witnessed_effects_compose():
    """Theorem 11 (§3.1.2): correct undo buttons stay correct under
    composition.

    Plain words: if change A knows how to undo itself and change B knows how
    to undo itself, then "A after B" also knows how to undo itself — the
    composed undo (undo A, then undo B) really works.  The paper's
    Definition 8 witness is checked here directly by `is_witnessed`.
    """
    rng = random.Random(303)
    n = N
    for _ in range(300):
        e1 = ref.random_effect(rng, n, witnessed=True)
        e2 = ref.random_effect(rng, n, witnessed=True)
        assert ref.is_witnessed(e1)
        assert ref.is_witnessed(e2)
        assert ref.is_witnessed(ref.effect_compose(e1, e2))

    # clause 2 — a single global undo (g ∘ f = id) witnesses at EVERY state
    for _ in range(100):
        f, g = ref.random_witnessed_pair(rng, n)
        assert ref.compose(g, f) == ref.identity_fn(n)
        assert ref.is_witnessed(ref.induced_effect(f, g))


# ---------------------------------------------------------------------------
# The lifted effect — Definition 12, Theorems 13 and 15
# ---------------------------------------------------------------------------


def lifted_diamond(e1, e2, gamma, phi):
    """(effect(e1) ⋄ effect(e2)) evaluated at one ∂Γ-state.

    This is Definition 9's composition read one level up (on states of ∂Γ):
    run `e2` first, then `e1`; compose the lifted inverses in the opposite
    order.  Lifted inverses are `track` pairs, and composing two of them is
    the twisted composition of Definition 1.
    """
    state1, inv2 = ref.effect_lift(e2, gamma, phi)
    state2, inv1 = ref.effect_lift(e1, *state1)
    composite_inverse = ref.compose_pair(inv2, inv1)
    return state2, composite_inverse


def test_theorem_13_effect_lift_preserves_diamond():
    """Theorem 13 (§3.1.2): lifting effects to the tracked level and then
    composing equals composing and then lifting.

    Plain words: tracking is compatible with sequencing — a runtime that
    tracks each effect as it happens ends up with exactly the same
    accumulated undo as one that had somehow composed the whole sequence
    first.  Pointwise at every state of ∂Γ.
    """
    rng = random.Random(404)
    n = N
    for _ in range(120):
        e1 = ref.random_effect(rng, n, witnessed=False)
        e2 = ref.random_effect(rng, n, witnessed=False)
        composed = ref.effect_compose(e1, e2)
        for gamma in range(n):
            for phi in [ref.identity_fn(n), ref.random_function(rng, n)]:
                left_state, left_inv = lifted_diamond(e1, e2, gamma, phi)
                right_state, right_inv = ref.effect_lift(composed, gamma, phi)
                assert left_state == right_state
                assert left_inv == right_inv


def test_theorem_15_lifted_inverse_recovers_state_exactly():
    """Theorem 15 (§3.1.2): pressing a single effect's lifted undo returns
    the world to exactly what it was; the *accumulator* is restored fully
    precisely when the effect carries one global undo (`g ∘ f = id`).

    Plain words: withdrawing one change in isolation is safe — the world
    comes back perfectly.  The bookkeeping (earlier undos) also comes back
    untouched when the effect's undo is globally correct; otherwise the
    bookkeeping may drift, but the paper proves that drift never affects
    what the accumulator ultimately restores (Theorem 7).
    """
    rng = random.Random(505)
    n = N
    ident = ref.identity_fn(n)

    witnessed_restores_state = 0
    for _ in range(300):
        e = ref.random_effect(rng, n, witnessed=True)
        gamma = rng.randrange(n)
        phi = ref.random_function(rng, n)
        delta, g = e[gamma]
        lifted_state, lifted_inverse = ref.effect_lift(e, gamma, phi)
        restored = ref.lifted_inverse_apply(lifted_inverse, lifted_state)
        # the world is recovered exactly ...
        assert restored[0] == gamma
        witnessed_restores_state += 1
        # ... and the accumulator iff g ∘ f = id (checked, not assumed)
        if ref.compose(g, ref.effect_forward(e)) == ident:
            assert restored[1] == phi
        else:
            assert restored[1] == ref.compose(ref.compose(phi, g), ref.effect_forward(e))
    assert witnessed_restores_state > 0


# ---------------------------------------------------------------------------
# LIFO recovery — Theorem 16
# ---------------------------------------------------------------------------


def test_theorem_16_lifo_recovery():
    """Theorem 16 (§3.1.2): apply a sequence of correctly-undoable changes,
    then press the undo buttons in reverse order — every intermediate world
    is revisited exactly, and you land where you started.

    Plain words: this is the familiar "stack of cleanup handlers" behavior,
    here derived rather than assumed: each undo meets precisely the world
    its own change produced.
    """
    rng = random.Random(606)
    n = N
    for _ in range(200):
        count = rng.randrange(1, 6)
        effects = [ref.random_effect(rng, n, witnessed=True) for _ in range(count)]
        gamma0 = rng.randrange(n)

        # apply in order, remembering the world before each change
        worlds = [gamma0]
        for e in effects:
            worlds.append(e[worlds[-1]][0])

        # revert in reverse order; each undo must return to the world that
        # preceded its own application
        state = worlds[-1]
        for i in reversed(range(count)):
            inverse = effects[i][worlds[i]][1]
            state = inverse[state]
            assert state == worlds[i]


# ---------------------------------------------------------------------------
# Independence — Definitions 17 and 19, Theorem 20, Corollary 21
# ---------------------------------------------------------------------------


def independent_pair_induced(f1, g1, f2, g2):
    """Definition 19 for pair-induced effects, which (as the paper notes)
    reduces to the four cross-commutations of the two changes and undos —
    clause (2) holds automatically when each effect has one global undo."""
    return (
        ref.commutes(f1, f2)
        and ref.commutes(f1, g2)
        and ref.commutes(g1, f2)
        and ref.commutes(g1, g2)
    )


def test_theorem_20_independent_withdrawal():
    """Theorem 20 (§3.1.3): when two changes are *independent* (all their
    parts commute), withdrawing one leaves exactly the other's contribution.

    Plain words: if change A and change B do not interfere, then undoing A
    after both ran — even though B's change sits "on top" — produces the
    same world as if A had never happened, with B intact.  This is what
    makes unplugging ONE plugin safe while the others keep running.
    """
    rng = random.Random(707)
    n = N
    found = 0
    while found < 40:
        f1, g1 = ref.random_witnessed_pair(rng, n)
        f2, g2 = ref.random_witnessed_pair(rng, n)
        if not independent_pair_induced(f1, g1, f2, g2):
            continue
        found += 1
        for gamma0 in range(n):
            both = f2[f1[gamma0]]
            only2 = f2[gamma0]
            # withdrawing e1 from the composite state reaches "just e2"
            assert g1[both] == only2
            # and withdrawing e2 from that reaches the start
            assert g2[only2] == gamma0


def test_corollary_21_any_order_recovery():
    """Corollary 21 (§3.1.3): for pairwise-independent changes, the undo
    buttons work in ANY order — not just last-in-first-out.

    Plain words: LIFO always works (Theorem 16), but independence buys more:
    the system may unplug components in whatever order is convenient — for
    example, waiting for a dependent to finish before its provider — without
    breaking recovery.  This is the license the loader's teardown ordering
    rests on.
    """
    rng = random.Random(808)
    n = N
    completed = 0
    while completed < 25:
        count = 3
        pairs = []
        while len(pairs) < count:
            candidate = ref.random_witnessed_pair(rng, n)
            if all(
                independent_pair_induced(pairs[i][0], pairs[i][1], candidate[0], candidate[1])
                for i in range(len(pairs))
            ):
                pairs.append(candidate)
        completed += 1

        for gamma0 in range(n):
            # apply in order, collecting the undo handed out at each state
            state = gamma0
            undos = []
            for f, g in pairs:
                undos.append(g)
                state = f[state]

            # press the undos in several random permutations; all must land home
            for _ in range(5):
                order = list(range(count))
                rng.shuffle(order)
                final = state
                for i in order:
                    final = undos[i][final]
                assert final == gamma0


def test_independence_is_a_real_condition():
    """The flip side of Corollary 21 (illustrated in §3.3.2's discussion):
    when the hypothesis fails, undo order starts to matter.

    Plain words: two plugins that both "restore the whole table to what they
    saw" each have a perfectly correct undo button (Definition 8 is
    satisfied!) — yet undoing them in the wrong order leaves the other's
    work destroyed.  Independence is a condition on *how* effects are built,
    which is why the paper pins it on the coeffect interface (a table you
    add to and remove from is fine; an ordered chain is not).
    """
    # the shared world: a table of registered routes (an ORDERED chain —
    # each registration snapshots and restores the whole list)
    def register(routes: tuple, name: str):
        new_routes = routes + (name,)

        def undo(current: tuple) -> tuple:
            return routes  # restore everything to what I saw

        return new_routes, undo

    routes: tuple = ()
    routes, undo_a = register(routes, "A")
    routes, undo_b = register(routes, "B")
    assert routes == ("A", "B")

    # LIFO works: undo B (restores ["A"]), then undo A (restores [])
    lifo = undo_a(undo_b(routes))
    assert lifo == ()

    # non-LIFO breaks: undo A first wipes B's registration from existence
    broken = undo_b(undo_a(routes))
    assert broken == ("A",)  # B's undo "restores" a world where B exists!


# ---------------------------------------------------------------------------
# The coeffect context Σ — Definitions 23, 25, 26, Theorem 40
# ---------------------------------------------------------------------------


def test_definition_23_set_and_get_preconditions():
    """Definition 23 (§3.2.1): provision requires the name to be free;
    reading requires it to be present; and provision is a fully revertible
    effect (its undo removes exactly what it added)."""
    sigma = {}
    sigma, undo = ref.coeffect_set(sigma, "database", {"url": "db://1"})
    assert ref.coeffect_get(sigma, "database") == {"url": "db://1"}

    # a name cannot be provided twice — the single-source discipline
    try:
        ref.coeffect_set(sigma, "database", {"url": "db://2"})
        raise AssertionError("second provision must be refused")
    except KeyError:
        pass

    # reading an absent name is refused
    try:
        ref.coeffect_get(sigma, "cache")
        raise AssertionError("absent read must be refused")
    except KeyError:
        pass

    # ... and withdrawal removes exactly what provision added
    assert undo(sigma) == {}


def test_definition_26_classification_trichotomy():
    """Definition 26 (§3.2.2): every table change lands in exactly one of
    three buckets against a component's declared dependencies.

    Plain words: this little classifier IS reactivity — it is how the system
    decides "you may start now" (activating), "you must stop now"
    (deactivating) or "nothing changed for you" (neutral).
    """
    sigmas = [{}, {"a": 1}, {"b": 2}, {"a": 1, "b": 2}, {"a": 1, "b": 2, "c": 3}]
    specs = [{"a"}, {"b"}, {"a", "b"}, {"c"}, set()]

    for before in sigmas:
        for after in sigmas:
            for deps in specs:
                got = ref.classify(before, after, deps)
                assert got in ("activating", "deactivating", "neutral")
                was = ref.satisfies(before, deps)
                now = ref.satisfies(after, deps)
                if got == "activating":
                    assert (not was) and now
                elif got == "deactivating":
                    assert was and (not now)
                else:
                    assert was == now


def test_theorem_40_distinct_keys_are_independent():
    """Theorem 40 (§3.3.2): operations on *different* dependency names never
    interfere — the paper's formal version of "registering a route and
    registering an event listener don't conflict".

    Together with Corollary 21 this licenses unplugging either provider in
    any order.  (Contrast `test_independence_is_a_real_condition` above for
    what happens when two effects share one ordered structure.)
    """
    base = {"routes": (), "listeners": ()}

    def register(key, name):
        def forward(sigma):
            return {**sigma, key: sigma[key] + (name,)}

        def undo(sigma):
            entries = tuple(e for e in sigma[key] if e != name)
            return {**sigma, key: entries}

        return forward, undo

    f_route, u_route = register("routes", "/home")
    f_listen, u_listen = register("listeners", "ready")

    # the two orders produce the same world — commutation of distinct keys
    assert f_route(f_listen(base)) == f_listen(f_route(base))

    # interleaved application, then undo in EITHER order, restores the base
    world = f_route(f_listen(base))
    assert u_route(u_listen(world)) == base
    assert u_listen(u_route(world)) == base
