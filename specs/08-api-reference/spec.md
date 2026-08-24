---
spec_id: 08-api-reference
status: CLOSED
closed_as: SHIPPED
since: 2026-08-24
until: null
epic: teaching
features: [quartodoc-reference, docstring-coverage]
supersedes: []
superseded_by: null
depends_on: [06-guide-gaps]
anchors: [kernel-architecture]
---

# The API reference is generated from the source

# 1 · Requirements

## Introduction

Spec 06 produced a hand-built `api-reference.qmd`: every public name and the first line
of its docstring, from a bespoke script. That is a *name index*. It answers "does
this exist", and not one of the questions someone actually opens a reference to
ask — what arguments does it take, what does it return, what are the methods on
this service, what does this parameter mean.

A model can read `src/` and answer those. A human reading the published site
cannot, and telling a reader to open the source is telling them the docs do not
cover it.

There is a second reason, which is what a generated reference is *for* beyond
lookup: it is an audit of the public surface. A name with no docstring, two names
doing one job, a docstring describing an API that no longer exists — none of
those are visible while the reference is written by hand from a list of names.
They become visible the moment the page is generated from the source.

## Glossary

- **quartodoc** — the standard Quarto mechanism for Python API docs. Reads
  signatures and docstrings with griffe and emits `.qmd`, so the reference lands
  in the same site, theme and search index as the guide.
- **Attribute docstring** — a string literal directly below a module-level
  assignment. The only way a constant carries documentation a generator can read.

## Mental model & invariants

1. **A reference is generated or it is wrong.** Anything hand-maintained beside
   the code drifts from it; the previous page listed `CONTEXT_MEMBERS` with
   `frozenset`'s docstring — "Build an immutable unordered collection of unique
   elements" — because a script asked the object for its first docstring line and
   got its base class's.
2. **The generator only sees what the source says.** quartodoc omits an
   undocumented member entirely, so a missing docstring is a missing entry, with
   no error anywhere. That makes docstring coverage a property to test, not a
   style preference.

**Invariants:**

- **I1** Every name in `plugkit.__all__` appears in the reference with its
  signature.
- **I2** Every public method of an exported class has a docstring, so it cannot
  vanish from the page silently.
- **I3** The reference is one command to rebuild, and that command is documented
  where the other build commands are.

## Requirements

### Requirement 1: a generated reference, in the existing site

**User story:** As someone evaluating or using plugkit, I want signatures and
parameter descriptions on the docs site, so that I do not have to read the source
to call a function.

1. THE reference SHALL be generated from signatures and docstrings by a standard
   tool rather than by a bespoke script.
2. THE reference SHALL render in the same site, theme, navigation and search as
   the guide.
3. WHEN a name is exported and absent from the reference, THE build SHALL fail
   rather than emit a page missing it.
4. THE hand-written page and its script SHALL be deleted, not kept alongside.

### Requirement 2: the surface is documented enough to be documentable

1. WHEN a public method has no docstring, THE suite SHALL fail, naming it.
2. WHEN a module-level exported constant is documented, IT SHALL use an
   attribute docstring, since a comment is invisible to the generator.

### Non-functional

- **NF1** `quartodoc` is a documentation-time dependency. `dependencies = []`
  stays true, and `test_bare_install.py` keeps passing.
- **NF2** The one-page guide build takes the reference as its appendix, so it
  cannot go on citing a page that no longer exists.

## Out of scope

- **Docstring style.** griffe parses the Google-style sections the codebase
  already uses; converting the prose docstrings to numpydoc is not worth doing.
- **Per-symbol pages.** `style: single-page` keeps the reference one page, which
  suits a 39-name surface and lets the one-page guide embed it whole.

# 2 · Design

`quartodoc:` block in `docs/_quarto.yml` holds the section layout — which names
appear and under which heading — and `scripts/build-reference.py` runs the tool
and does three things around it that quartodoc does not: fix the output filename
(the single-page builder writes `index.qmd.qmd`), prepend frontmatter, and check
`__all__` coverage. It also de-links anchors the page does not define, which
would otherwise be dead clicks on the published site.

# 3 · Tasks

## Status marks
<!-- [ ] pending | [x] done | [!] BLOCKED: reason | [-] DROPPED: <reason> | [>] → <spec_id> -->

## Tasks

- [x] 1. Generated reference
  - [x] 1.1 quartodoc config and build script
    - **Requirements**: 1.1, 1.2, 1.3
    - **Pillar**: Documentation
    - `docs/_quarto.yml` `quartodoc:` block, `scripts/build-reference.py`,
      output at `docs/reference/index.qmd`. Wired into render list, navbar and
      sidebar; `quarto render` verified over all 18 pages.
  - [x] 1.2 Retire the hand-written page
    - **Depends**: 1.1
    - **Requirements**: 1.4
    - **Pillar**: Documentation
    - Deleted the old `api-reference.qmd` and its `build-api-reference.py`.
      `test_api_reference_agrees_with_all` now reads the generated anchors.
      Spec 06 carries a forward pointer, since an archival record whose paths
      no longer resolve stops being a record.
  - [x] 1.3 The one-page guide takes the generated reference
    - **Depends**: 1.1
    - **Requirements**: NF2
    - **Pillar**: Documentation

- [x] 2. Docstring coverage
  - [x] 2.1 Document every public method
    - **Requirements**: 2.1
    - **Pillar**: Code, Documentation
    - Twenty were undocumented and therefore absent from the reference,
      including `ConfigService.load_yaml` and `load_dict`, which chapter 5
      teaches, and `Fiber.effect`, which is the mechanism the whole design rests
      on. `CONTEXT_MEMBERS` and `DIAGNOSTICS` gained attribute docstrings (R2.2).
  - [x] 2.2 A test that keeps it that way
    - **Depends**: 2.1
    - **Requirements**: 2.1
    - **Pillar**: Test
    - `test_every_public_method_has_a_docstring` in `test_docs_consistency.py`.

## Notes

Findings the generated page surfaced, beyond the missing docstrings:

- `is_stale`'s docstring showed `self.rt.items.get()` and an `@effect` decorator
  — an API from the retired predecessor project. Rewritten against the current
  one. A wrong example in a reference is worse than no example, and nothing but
  publishing it would have found it.
- `CONTEXT_MEMBERS` was published carrying `frozenset`'s docstring.
- `Signal.value` is a property alias for `get()` — duplicate surface, and the one
  anchor the generator links without emitting. Left alone here; it is an API
  decision, not a docs one.
- `ConfigService.signal_for` hands a caller the raw `Signal`, which is the
  implementation detail `07-change-notification` just finished hiding behind
  `watch`. Also an API decision, also left alone.

## Log

**2026-08-24** — Opened and closed SHIPPED. quartodoc replaces the bespoke
name-index page; twenty undocumented public methods documented and guarded by a
test.
