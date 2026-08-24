"""An executable model of the Cordis paper's Section 3 ("revertible effects
and reactive coeffects").

This module is deliberately independent of the `cordis` package: it is a
direct translation of the paper's *mathematics* into small Python functions,
so the tests in this folder can check the theorems against the math itself,
and separately check that the real runtime agrees with the math.

A plain-language guide to the vocabulary
=========================================

**The context Γ ("gamma").**  Everything a running system shares — think of
it as "the state of the world": allocated memory, registered event
listeners, open connections.  The paper stays abstract about what Γ is; to
*test* anything we need a concrete stand-in, so this module uses a tiny
finite world: the integers `0 .. n-1` (see `all_functions`).  A finite world
means we can sometimes check a theorem on *every* possible case instead of a
sample — the closest a test suite can get to a proof.

**A transformation.**  A function `Γ -> Γ`: something that takes the world
and returns a new world.  "Register a listener" is a transformation; so is
"unregister it".  In the finite world a transformation is just a tuple where
`fn[i]` says where state `i` is sent.

**An effect (Definition 8).**  In everyday terms: *a change, bundled with
its own undo button*.  Formally it is a function that takes the current
world `γ` and returns a pair `(δ, g)`: the new world `δ` and an inverse `g`
that carries `δ` back to `γ`.  The paper calls an effect *witnessed*
(`𝔈Γ*`) when the undo button really works: `g(δ) == γ` for every input.

**The accumulator φ ("phi").**  One big composed undo button.  As effects
are applied, their individual undo buttons are composed (chained) into a
single function.  "Unplug the component" then means "press the big button".

**The effect context ∂Γ (Definition 2).**  The pair `(γ, φ)`: what the world
is now, plus the accumulated undo of everything done so far.

**track / recover (Definitions 3 and 6).**  `track(f, g)` applies the change
`f` and *prepends* the undo `g` onto the accumulator.  `recover` presses the
accumulator and resets it.  Theorem 7 — the "soundness invariant" — says
pressing the accumulator always returns you to where you started, provided
each undo button really undoes its own change.

**Coeffects (Section 3.2).**  The dual idea: instead of *what a component
changes*, it is *what a component needs*.  A component declares a
specification `d` (a set of service names); the world carries a table `σ`
("sigma") mapping names to values.  The specification is *satisfied*
(`σ ⊨ d`) when every declared name is present.  Every change to the table
can be classified against `d` as *activating* (needs just appeared),
*deactivating* (needs just vanished) or *neutral* — that is `classify`, the
paper's Definition 26, and it is what makes dependencies "reactive".
"""

from __future__ import annotations

import itertools
import random
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# The finite world Γ and its transformations
# ---------------------------------------------------------------------------


def all_functions(n: int) -> list[tuple]:
    """Every transformation of the finite world `{0..n-1}`.

    A transformation is written *extensionally* as a tuple of images:
    `fn[i]` is where state `i` is sent.  There are `n**n` of them, so with
    `n = 3` we get 27 — small enough to enumerate exhaustively in tests.
    """
    return list(itertools.product(range(n), repeat=n))


def identity_fn(n: int) -> tuple:
    """`idΓ` — the do-nothing transformation."""
    return tuple(range(n))


def apply_fn(fn: tuple, x: int) -> int:
    """Apply a transformation to a state."""
    return fn[x]


def compose(f: tuple, g: tuple) -> tuple:
    """`(f ∘ g)(x) = f(g(x))` — do `g` first, then `f`."""
    return tuple(f[g[x]] for x in range(len(f)))


def commutes(f: tuple, g: tuple) -> bool:
    """Whether two transformations commute: `f(g(x)) == g(f(x))` everywhere.

    Commutation is the paper's notion of "these two changes do not interfere
    with each other" — undoing one never disturbs the other, whichever order
    they ran in (Definition 19 makes this precise).
    """
    return all(f[g[x]] == g[f[x]] for x in range(len(f)))


def compose_pair(pair1: tuple, pair2: tuple) -> tuple:
    """The *twisted* composition of Definition 1.

    A "pair" here is `(forward, inverse)`.  Composing `pair1 ∘ pair2` means
    "run pair2's change first, then pair1's" — so the forward parts compose
    in that order, but the *undo* parts accumulate in the opposite order
    (undo the later change first).  This opposite-order accumulation is
    exactly last-in-first-out (LIFO) cleanup.
    """
    f1, g1 = pair1
    f2, g2 = pair2
    return (compose(f1, f2), compose(g2, g1))


# ---------------------------------------------------------------------------
# The effect context ∂Γ, `track` and `recover` (Definitions 2, 3, 6)
# ---------------------------------------------------------------------------
#
# A state of ∂Γ is `(gamma, phi)` where `gamma` is the current world and
# `phi` is the accumulator — one composed undo function.  Both are plain
# Python values in the finite world: an int and a tuple.


def track(f: tuple, g: tuple) -> Callable[[tuple], tuple]:
    """Definition 3 — apply the change `f`, prepend the undo `g`.

        track(f, g) (γ, φ) = (f(γ), φ ∘ g)

    "Prepend" matters: the newest undo is pressed *first* when the
    accumulator runs, giving LIFO recovery.
    """

    def step(state: tuple) -> tuple:
        gamma, phi = state
        return (f[gamma], compose(phi, g))

    return step


def recover(state: tuple) -> tuple:
    """Definition 6 — press the accumulator and reset it.

        recover(γ, φ) = (φ(γ), idΓ)
    """
    gamma, phi = state
    return (phi[gamma], identity_fn(len(phi)))


def all_partial_states(n: int) -> list[tuple]:
    """Every state of ∂Γ: every world × every possible accumulator."""
    fns = all_functions(n)
    return [(gamma, phi) for gamma in range(n) for phi in fns]


# ---------------------------------------------------------------------------
# Effect functions (Definition 8) and their composition ⋄ (Definition 9)
# ---------------------------------------------------------------------------
#
# An effect function is represented extensionally as a tuple: `e[γ]` is the
# pair `(δ, g)` — the new world and the undo that carries it back.  (The
# paper writes `pr1` for "take the new world" and `pr2` for "take the undo".)


def effect_unit(n: int) -> tuple:
    """`ηΓ` — the no-op effect: change nothing, undo with the identity."""
    ident = identity_fn(n)
    return tuple((gamma, ident) for gamma in range(n))


def effect_forward(e: tuple) -> tuple:
    """`pr1 ∘ e` — just the "new world" part of an effect, as a plain
    transformation of Γ."""
    return tuple(delta for delta, _ in e)


def effect_inverse_at(e: tuple, gamma: int) -> tuple:
    """`pr2(e(γ))` — the undo button this effect hands out at world `γ`."""
    return e[gamma][1]


def effect_compose(f: tuple, g: tuple) -> tuple:
    """Definition 9 — `f ⋄ g`, "run `g` first, then `f`".

        (f ⋄ g)(γ) = let (δ, s) = g(γ) in let (ε, t) = f(δ) in (ε, s ∘ t)

    The composed undo presses `f`'s undo before `g`'s — again LIFO: whatever
    was done last is undone first.
    """
    out = []
    for gamma in range(len(f)):
        delta, s = g[gamma]
        epsilon, t = f[delta]
        out.append((epsilon, compose(s, t)))
    return tuple(out)


def induced_effect(f: tuple, g: tuple) -> tuple:
    """The effect "apply `f`, undo with `g`" — Theorem 10.2's embedding of a
    plain (change, undo) pair into a full per-state effect function."""
    return tuple((f[gamma], g) for gamma in range(len(f)))


def is_witnessed(e: tuple) -> bool:
    """Definition 8 — is this effect's undo button actually correct?

    "Witnessed" means: at *every* world `γ`, the returned undo really carries
    the new world back to `γ`.  (The runtime cannot check this for you; it is
    the component author's promise.  Here we can just check it.)
    """
    return all(g[delta] == gamma for gamma, (delta, g) in enumerate(e))


def effect_lift(e: tuple, gamma: int, phi: tuple) -> tuple:
    """Definition 12 — `effectΓ(e)` evaluated at one state of ∂Γ.

    Returns `((δ, φ ∘ g), (g, pr1 ∘ e))`: the new ∂Γ-state (world advanced,
    undo prepended) together with the *lifted* inverse — which is itself a
    `track` pair: "undo `g`; to undo *that*, redo the effect's forward part".
    The tests only ever need this evaluated pointwise.
    """
    delta, g = e[gamma]
    new_state = (delta, compose(phi, g))
    lifted_inverse = (g, effect_forward(e))
    return new_state, lifted_inverse


def lifted_inverse_apply(pair: tuple, state: tuple) -> tuple:
    """Run a lifted inverse (a `track` pair) on a ∂Γ-state — see `track`."""
    return track(*pair)(state)


# ---------------------------------------------------------------------------
# Effect iterators (Definition 51) — a multi-step activation
# ---------------------------------------------------------------------------
#
# An *iterator* models a component whose activation performs several changes
# one after another, pausing between them (in real code: a generator or an
# async function that `yield`s its undo buttons as it goes).  Here an
# iterator is a function `γ -> (δ, g, cont)` where `cont` is either `None`
# ("Nothing" — done) or the next iterator ("Just i").  The witness condition
# is the same as for one-shot effects, checked at every step.


def iterate(effect_iter: Callable, gamma: int) -> tuple:
    """Run an iterator to completion from `gamma`.

    Returns `(final_gamma, undo_stack)` where `undo_stack` lists the undo
    buttons in the order they were handed out (so *reversed*, it is the
    order they should be pressed in — LIFO again, Definition 52).
    """
    undos: list[tuple] = []
    current: Optional[Callable] = effect_iter
    state = gamma
    while current is not None:
        delta, g, cont = current(state)
        undos.append(g)
        state = delta
        current = cont
    return state, undos


def iterator_witnessed(effect_iter: Callable, gamma: int) -> bool:
    """Check the Definition 51 witness along one run: every handed-out undo
    really returns the world to what it was just before that step."""
    current: Optional[Callable] = effect_iter
    state = gamma
    while current is not None:
        delta, g, cont = current(state)
        if g[delta] != state:
            return False
        state = delta
        current = cont
    return True


# ---------------------------------------------------------------------------
# Independence (Definitions 17 and 19) — when changes do not interfere
# ---------------------------------------------------------------------------


def generated_monoid(generators: list[tuple], n: int) -> set[tuple]:
    """Definition 17 — everything obtainable by composing the generators.

    The paper's 𝔐(e): the forward changes and undo buttons of an effect,
    closed under composition.  In a finite world this set is finite (at most
    `n**n` elements), so "every element of one monoid commutes with every
    element of the other" — the paper's Definition 19 clause (1) — becomes a
    finite, decidable check rather than an abstract condition.
    """
    ident = identity_fn(n)
    seen = {ident}
    frontier = [ident]
    gens = [g for g in generators if g != ident]
    while frontier:
        new_frontier = []
        for elem in frontier:
            for gen in gens:
                nxt = compose(elem, gen)
                if nxt not in seen:
                    seen.add(nxt)
                    new_frontier.append(nxt)
        frontier = new_frontier
    return seen


# ---------------------------------------------------------------------------
# The coeffect context Σ (Definitions 22, 23, 25, 26)
# ---------------------------------------------------------------------------
#
# Here the "world" is a dictionary mapping service names to values — the
# paper's dependency table σ.  This is exactly what `cordis.ReflectService`
# implements at runtime; this copy exists so the theorems about it can be
# checked against the math directly.


Sigma = dict


def coeffect_set(sigma: Sigma, key: str, value) -> tuple:
    """Definition 23 — `set(k, v)`: an *effect* on the dependency table.

    Takes the table, returns `(new_table, undo)`.  The precondition is that
    `k` is not already present (a service cannot be provided twice); the
    undo removes it again — so provision is a fully revertible effect.
    """
    if key in sigma:
        raise KeyError(f"precondition violated: {key!r} already provided")
    new_sigma = dict(sigma)
    new_sigma[key] = value

    def undo(s: Sigma) -> Sigma:
        del s[key]
        return s

    return new_sigma, undo


def coeffect_get(sigma: Sigma, key: str):
    """Definition 23 — `get(k)`; requires the key to be present."""
    if key not in sigma:
        raise KeyError(f"precondition violated: {key!r} not provided")
    return sigma[key]


def satisfies(sigma: Sigma, deps) -> bool:
    """Definition 25's satisfaction predicate: `σ ⊨ d` — every declared
    dependency is present in the table."""
    return all(key in sigma for key in deps)


def classify(sigma: Sigma, sigma_next: Sigma, deps) -> str:
    """Definition 26 — `notify_d(σ, σ')`: how a table change lands against a
    component's declared dependencies.

    Returns exactly one of `"activating"` (needs just became available —
    the component may start), `"deactivating"` (needs just vanished — the
    component must stop) or `"neutral"` (no change that matters).
    """
    before = satisfies(sigma, deps)
    after = satisfies(sigma_next, deps)
    if not before and after:
        return "activating"
    if before and not after:
        return "deactivating"
    return "neutral"


# ---------------------------------------------------------------------------
# Random generation, seeded and reproducible
# ---------------------------------------------------------------------------
#
# Property tests need inputs.  Everything here uses `random.Random(seed)` so
# a failure is always reproducible from the seed printed by pytest.


def random_function(rng: random.Random, n: int) -> tuple:
    return tuple(rng.randrange(n) for _ in range(n))


def random_effect(rng: random.Random, n: int, witnessed: bool = True) -> tuple:
    """A random effect function on the finite world.

    With `witnessed=True` the undo buttons are *correct by construction*:
    at each world we pick a new world `δ` and build an undo that sends `δ`
    back where it came from (its other entries are random — the paper only
    requires the undo to work at the world it was handed out for).
    """
    entries = []
    for gamma in range(n):
        delta = rng.randrange(n)
        if witnessed:
            images = [rng.randrange(n) for _ in range(n)]
            images[delta] = gamma
            g = tuple(images)
        else:
            g = random_function(rng, n)
        entries.append((delta, g))
    return tuple(entries)


def random_witnessed_pair(rng: random.Random, n: int) -> tuple:
    """A random (change, undo) pair whose undo really works everywhere —
    `g ∘ f = id`.  Built by taking a random bijection and its inverse."""
    perm = list(range(n))
    rng.shuffle(perm)
    f = tuple(perm)
    g = tuple(perm.index(x) for x in range(n))  # inverse permutation
    return f, g
