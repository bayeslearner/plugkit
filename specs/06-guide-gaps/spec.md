---
spec_id: 06-guide-gaps
status: DRAFT
closed_as: null
since: 2026-08-24
until: null
epic: teaching
features: [supervision-chapter, api-reference]
supersedes: []
superseded_by: null
depends_on: [05-doc-code-consistency]
anchors: [kernel-architecture]
---

# The guide covers what ships

# 1 · Requirements

## Introduction

The guide has eight chapters and the package has six shipped services. One of
them — `ctx.supervisor` — appears in the README table, in the CHANGELOG, and in
`__all__`, and nowhere in the guide. A reader following the book end to end never
learns it exists.

The audit in spec 05 found this through a broken cross-reference: chapter 1 said
"that is also how supervision sees failures" and linked to the tools chapter,
because there was no supervision chapter to link to. Spec 05 fixed the sentence.
This spec fixes the reason it was wrong.

`docs/steering/pillars.md` names the second gap itself: no API reference page.

## Glossary

- **Restart strategy** — what a supervisor does to sibling fibers when one fails.
  `one_for_one` restarts only the failed fiber; `one_for_all` restarts every
  supervised fiber; `rest_for_one` restarts the failed one and everything started
  after it.
- **Escalation** — giving up rather than restarting forever, once a fiber has
  exceeded its restart budget.

## Mental model & invariants

1. **A shipped service with no chapter is a service nobody will use.** The README
   table says it exists; the guide is what teaches someone to reach for it.
2. **Supervision is the payoff of `FAILED`, not a separate subsystem.** Chapter 1
   already teaches the six fiber states. The chapter should land as the answer to
   "so what do I do about `FAILED`", not as a new vocabulary.

**Invariants:**

- **I1** Every service in `plugkit.__all__` that a user is expected to mount is
  taught in the guide or is explicitly listed as intentionally undocumented.
- **I2** Every example in the new chapter is executed by the suite, as the other
  eight chapters are.

## Requirements

### Requirement 1: supervision has a chapter

**User story:** As a reader, I want to know what to do when a plugin fails, so
that I do not have to read `services/supervision.py` to find out.

1. WHEN a reader finishes the guide, THEY SHALL have seen how a failed fiber is
   restarted and how a restart budget escalates.
2. WHEN the chapter shows an example, THE suite SHALL execute it.
3. THE chapter SHALL be reachable from chapter 1's discussion of the `FAILED`
   state.

### Requirement 2: an API reference exists

1. WHEN a reader wants a signature rather than a narrative, THE docs SHALL offer
   a reference page covering the public surface.
2. WHEN a name is added to `plugkit.__all__`, THE reference SHALL be checked
   against it by the suite.

### Non-functional

- **NF1** Adding a ninth chapter changes the chapter count stated in
  `docs/steering/pillars.md`. Either restate it or stop stating it, consistent
  with the rule spec 05 established for counts.

## Out of scope

- Restating the kernel. The chapter teaches the service, not the fiber
  lifecycle — chapter 1 owns that.

# 2 · Design

To be written at `/spec-plan 06-guide-gaps refine`, before any drafting. The
shape is expected to be: one chapter between the current 05 and 06 (renumbering
the later chapters and every cross-reference to them, which
`test_docs_consistency.py` will catch if missed), plus a reference page generated
from docstrings rather than hand-maintained.

# 3 · Tasks

## Status marks
<!-- [ ] pending | [x] done | [!] BLOCKED: reason | [-] DROPPED: <reason> | [>] → <spec_id> -->

## Tasks

- [ ] 1. Supervision chapter
  - [ ] 1.1 Draft the chapter against `services/supervision.py`
    - **Requirements**: 1.1, 1.3
    - **Pillar**: Teaching
  - [ ] 1.2 Every example executed by `test_guide_examples.py`
    - **Depends**: 1.1
    - **Requirements**: 1.2
    - **Pillar**: Teaching, Test
  - [ ] 1.3 Renumber later chapters and fix every cross-reference
    - **Depends**: 1.1
    - **Pillar**: Documentation

- [ ] 2. API reference
  - [ ] 2.1 A reference page over the public surface
    - **Requirements**: 2.1
    - **Pillar**: Documentation
  - [ ] 2.2 A check that `__all__` and the reference agree
    - **Depends**: 2.1
    - **Requirements**: 2.2
    - **Pillar**: Documentation, Test

## Notes

Carried from `05-doc-code-consistency` task 4.2.

## Log

**2026-08-24** — Opened to hold the supervision chapter deferred from spec 05.
