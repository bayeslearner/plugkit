# Publishing plugkit 0.1.0 — handoff

Written 2026-08-23. Everything below was measured, not assumed.

**Do not publish yet.** LO decides when. This records how, and what is worth
fixing first.

## What is already done

| | |
|---|---|
| `.github/workflows/publish-pypi.yml` | correct as written. Triggers on `v*` tags **and** `workflow_dispatch`. Uses trusted publishing — `id-token: write`, no token. |
| GitHub `pypi` environment | exists on `bayeslearner/plugkit`, no protection rules, so a run will not wait for approval |
| Repo secrets | none, and none needed |
| Build | `python -m build` succeeds; `twine check` passes both artifacts |
| Name | `plugkit` is free on PyPI (404 on `/pypi/plugkit/json`) |

`signalpy-kernel` 0.4.0 on PyPI is LO's (`bayeslearner@gmail.com`). `signalpy` is
a different author's — not ours, not related.

## The one manual step: a pending publisher

Trusted publishing binds five values, and two changed since `signalpy-kernel`:

| binding | old | new |
|---|---|---|
| PyPI project | `signalpy-kernel` | **`plugkit`** |
| GitHub repo | `bayeslearner/signalpy-kernel` | **`bayeslearner/plugkit`** |
| owner | `bayeslearner` | unchanged |
| workflow | `publish-pypi.yml` | unchanged |
| environment | `pypi` | unchanged |

PyPI has no rename for this. The binding lives on the project, and `plugkit` does
not exist as a project yet, so the mechanism is a **pending publisher** — a
trusted publisher registered for a project that does not exist, which the first
successful upload creates.

**https://pypi.org/manage/account/publishing/**

```
PyPI Project Name:  plugkit
Owner:              bayeslearner
Repository name:    plugkit
Workflow name:      publish-pypi.yml
Environment name:   pypi
```

This is a browser step. There is no CLI path — an API token would also have to be
created in the browser, so it saves nothing.

## The tag collision

`v0.1.0` **already exists on origin**, pointing at the predecessor's "first
public release". So do `v0.2.0`, `v0.4.0` and `v2.0.0` ("ReactPy Kernel v2.0.0").
None is reachable from `main`; the branch rewrite missed them because tags are
not branch-scoped. The GitHub releases page still shows that lineage.

Intact copies are already pushed to `predecessor/v0.1.0` … `predecessor/v2.0.0`,
with the annotated tag objects preserved, so **nothing is lost**. Those names do
not match the workflow's `v*` filter and will not trigger a publish.

Two ways forward. LO chooses.

**A — clear the names, then tag normally.** Needs LO's hand; deleting published
tags was blocked by the permission classifier, correctly.

```bash
git push origin :refs/tags/v0.1.0 :refs/tags/v0.2.0 :refs/tags/v0.4.0 :refs/tags/v2.0.0
git tag -d v0.1.0 v0.2.0 v0.4.0 v2.0.0
git tag -a v0.1.0 -m "plugkit 0.1.0" && git push origin v0.1.0
```

**B — publish with no tag at all.** The workflow has `workflow_dispatch`, so the
tag mess can wait:

```bash
gh workflow run publish-pypi.yml --repo bayeslearner/plugkit
```

B was the recommendation for a first release: it decouples shipping from a
cleanup that has its own risk.

## One decision before the first upload

**Tests are 49% of the wheel** — 281 KB of 574 KB uncompressed, 50 files, and
`plugkit.tests` becomes importable in every user's environment.

The argument for shipping them: `test_conformance.py` is the project's central
claim, and letting a user run it beats asking them to trust the README. 180 KB
compressed is not meaningful, and removing them in 0.2 breaks nothing real.

Either way it is one line in `pyproject.toml`, and it is much cheaper to decide
now than after a release exists.

## What is worth fixing first

Found by a fresh reading of the docs against the code on 2026-08-23. Ordered by
whether a user would hit it.

### 1. `points.get()` disagrees with its own docstring — a real bug

`services/points.py:168` says duplicate keys resolve as *"the last one added
wins, matching `last()`"*. It scans `entries()`, which is sorted by
`(order, seq)`, and takes the last match — so with differing `order` values it
returns by **order**, not arrival. Reproduced:

```python
root.points.add("p", "registered-first",  key="k", order=10)
root.points.add("p", "registered-second", key="k", order=1)

root.points.get("p", "k")    # 'registered-first'
root.points.last("p")        # 'registered-second'
```

No test catches it because every existing test uses the default `order=0`, where
the two agree. Fix the code to match the docstring (scan by `seq`), not the other
way round — `last()`'s arrival-order semantics are what the approver stack needs.

### 2. The documented test command does not reproduce the gate

`CLAUDE.md` gives the gate as `uv run pytest src/plugkit/tests -q`.
`uv run --extra config --extra hmr pytest` gives **15 failures**, all from a
missing `pyyaml` — the loader, include and config-yaml tests need
`--extra providers`. Whatever the documented invocation is, it has to be the one
that passes.

### 3. Stale counts, in four places

- `test_conformance.py` has **17** test functions. `CLAUDE.md:93` says 17;
  `README.md:698`, `specs/01-plugkit-kernel/spec.md:292` and
  `docs/steering/pillars.md:33` all still say thirteen.
- `README.md:705` claims `# 296 passed`. It is **366**. `pillars.md` also says
  296.

A number in prose drifts. Consider whether these should be counts at all.

### 4. `README.md:59-64` — the iPOPO sentence appears twice

Lines 59-60 say *"[iPOPO], a port of OSGi, is the main exception."* Line 62
repeats it verbatim and continues. An editing artefact from this session.

### 5. A dangling cross-reference, and a missing chapter

`docs/guide/01-first-plugin.qmd:245` — *"That is also how supervision sees
failures"* links to the **tools** chapter. There is no supervision chapter.
`SupervisorService` is exported and in the README table but appears nowhere in
the eight guide chapters.

### 6. `binding.py:298` — a mount's config can silently shadow an injected service

`kwargs.update(plugin_config)` runs after the injected services are placed, so a
config key sharing a name with something in `needs` replaces it with no warning.
Not mentioned in any doc. Decide whether that is the intended precedence and then
say so, or reject the collision.

### 7. `binding.py:222` — `__exit__` without `__enter__`

`_find_closer` returns `obj.__exit__(None, None, None)` for a context manager the
binding never entered. Chapter 2 advertises the context-manager protocol as
supported teardown. Either call `__enter__` at construction or stop advertising
it.

## Suggested order

1–2 before publishing: one is a wrong answer at runtime, the other means nobody
can reproduce the gate. 3–5 are cheap and are what a first visitor reads. 6–7 are
design decisions and can wait for a spec.

The sprint queue is empty — specs 01 to 04 are all CLOSED/SHIPPED — so this is
spec 05's material.
