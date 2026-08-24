"""The Cordis paper, rendered as a test suite.

The paper ("A Programming Paradigm for Spatiotemporal Composability") is
written in the style of programming-language research: definitions,
theorems and inference rules.  This package makes that content *executable*
— each formal statement becomes one or more tests — so that

  * the mathematics itself can be checked by running it (against a small
    reference model in `reference.py`, independent of the library), and
  * the real `cordis` runtime can be held against the paper, rule by rule.

You do not need any programming-language-theory background to read these
tests; every module explains its statements in plain words.  The short
version of the paper's story:

  * A running system is a shared blob of state — registered listeners,
    open connections, configuration.  The paper calls it *the context*.
  * A **plugin** (the paper: *component*) is a bundle of code that changes
    that shared state and needs things from it.  One running copy of a
    plugin is a ***fiber***.
  * **Temporal composability** — the "time" dimension — means you can
    *unplug* a plugin and everything it did is undone, cleanly, as if it
    had never been there.  The trick: every change must come with its own
    undo button, and the runtime chains those buttons automatically
    (newest first).  The paper calls these *revertible effects*.
  * **Spatial composability** — the "space" dimension — means plugins can
    *declare what they need* ("I require a database") and the system wires
    them up, and re-wires them live as providers come and go.  The paper
    calls these *reactive coeffects*.
  * Section 4 of the paper pins down the resulting plugin lifecycle as ten
    precise rules — when a plugin may start, what happens when its
    dependencies vanish mid-start, how failures are contained — and then
    proves three global guarantees: the wiring never rots
    (*preservation*), the system always settles (*termination*), and the
    order things arrive in does not change where they end up
    (*confluence*).

The suite is organized in three tiers of increasing scope:

  `test_theorems.py`
      Tier 1 — the algebra of undo buttons (paper §3): checked against the
      reference model in `reference.py`, sometimes *exhaustively* over a
      finite world of states, which for those models is as good as a proof.

  `test_calculus.py`
      Tier 2 — the ten lifecycle rules (paper §4): each test stages the
      rule's premises against the real runtime and asserts its conclusion.
      Two tests are `xfail(strict=True)` on purpose: they assert the
      paper's reading of two rules that the shipped runtime (the original
      TypeScript and this port alike) does not implement — see README
      "Faithfulness notes".  If they ever XPASS, the runtime was aligned
      with the paper and the notes should be updated.

  `test_metatheory.py`
      Tier 3 — the global guarantees (paper §4.4): random programs of
      plugin operations are generated from fixed seeds; after every step
      the invariants are re-checked (preservation), every run must settle
      (termination), and the same program under different interleavings —
      and a from-scratch load of the final configuration — must agree
      (confluence).

What tests can and cannot tell you
----------------------------------

A theorem holds for *every* possible input; a test checks some inputs.  For
the finite models of Tier 1, "some" can mean "all", which is complete.  For
Tiers 2 and 3 the checks are samples — large, seeded and reproducible, but
samples.  Passing means the runtime agrees with the paper on everything
tried; a failure is always a concrete, replayable counterexample.  If you
need the theorems themselves rather than confidence in them, that is the
realm of proof assistants, not test suites; this package sits one step
below, keeping the formal content alive and executable.

Run it with:

    uv run pytest tests/paper
"""
