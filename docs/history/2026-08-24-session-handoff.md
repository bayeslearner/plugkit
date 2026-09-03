# Session handoff — 2026-08-24

For the next session. LO is reading the guide and code with ENI and wants bugs
found. Sessions are long; this preserves state so a fresh context can continue.

## Project state (verified, committed, pushed to main @ `5f7bf3a`)

All on `origin/main`. Suite: **379 passed, 3 skipped, 2 xfailed.**

**Specs 01–04 CLOSED/SHIPPED. `05-doc-code-consistency` CLOSED/SHIPPED** this
session (the doc↔code audit). **`06-guide-gaps` is DRAFT** — holds the deferred
supervision guide chapter (real teaching debt: `ctx.supervisor` ships with no
chapter). Next work lives there.

### What spec 05 did (three real code bugs + a mechanism)

1. `services/points.py` — `points.get()` resolved a duplicate key by `order`
   instead of arrival, disagreeing with `points.last()`. Invisible because all
   existing tests used the default `order=0`. Fixed to scan by `seq`.
2. `binding.py` — a mount config key could silently override an injected
   service (`kwargs.update(plugin_config)`); now raises. Also torn-down context
   managers without ever entering them; now `_setup_teardown` enters a sync CM
   (registers what `__enter__` returned) and refuses async-only CMs naming
   `close=`.
3. `test_docs_consistency.py` — the meta-deliverable. Drives I1–I6: links
   resolve, repo paths exist, stated conformance counts match the suite, no
   whole-suite test count in prose, and guide code blocks must import what they
   name. **Bug it caught: the conformance-assertion count was 17, not 13** —
   `CLAUDE.md` was right, README/spec-01/pillars/CHANGELOG were stale.

### The documented gate now reproduces

`uv run --extra dev pytest src/plugkit/tests -q`. `--extra dev` is non-optional
(needs pytest + pyyaml for the config-YAML tests); it is what CI installs.
`CLAUDE.md` and README were corrected.

## Docs rebuild — light theme by default, diagram fixed

`_quarto.yml` was dark-only with 400 lines of `!important` CSS. Now:

- `docs/theme-light.scss` — light colours (GitHub Light). **Default.**
- `docs/theme-dark.scss` — old dark colours, kept as a toggle.
- `docs/theme-neutral.css` — layout/shape only, zero colour.
- Quarto compiles a theme-bracket `.scss` only for the theme naming it, so the
  two halves never fight specificity. Light listed first = default.
- Verified in a browser: `#ffffff`/`quarto-light` on paint, toggle flips to
  `#1a1a2e`/`quarto-dark`, both legible.

### The diagram (finding #16)

The one mermaid block had **never rendered** — a plain ```` ```mermaid ````
fence emits `<pre class="mermaid">` but Quarto loads no runtime, so the page
published the diagram's source as text with no error. Reproduced in a 6-line
standalone file; confirmed it predated the theme work.

Fix: ```` ```{mermaid} ```` (executable form), which requires `.qmd` →
`docs/design/kernel-architecture.md` became `.qmd`. Every reference updated
(README, CLAUDE.md, `_quarto.yml`, guide 03). The `anchors:
[kernel-architecture]` slugs are NOT paths and were untouched. A test in
`test_docs_consistency.py` enforces it **scoped to rendered docs only** —
`specs/` is read on GitHub, which renders the plain form not the curly one.

**CI Quarto bumped `1.5.57` → `1.7.32`** in `publish-docs.yml` to match what
was verified locally. Pushing `5f7bf3a` triggered `publish-docs` on the new pin
— the next session should **check that GitHub Actions run** before trusting it.

## Unsolved / where the last exchange left off (incoming)

LO asked about **an artifact they believe I sent them earlier** — a
`claude.ai/code/artifact/...` link "based on the guide". I have **no record of
sending it** anywhere in this session or the repo (nothing was ever published,
and `docs/history/` is not rendered to the site). The link is a web-console
artifact, not something this CLI created. LO pushed back ("try again. can open
it now") and the session ended with a tool reload mid-attempt.

If it comes up again: the likely intent is **a single self-contained HTML page
assembled from the 8 guide chapters**, bright theme, runnable code. That is a
reasonable thing to build (`quarto` already produces each chapter; a
`guide.html` combining them is feasible), but **was never created** — so tell
LO plainly it isn't in the repo rather than pretending to find it, then offer
to build it.

## LO's standing framings for this project

- Reading the guide and code together; wants bugs explained against actual code,
  not memory.
- "Do NOT publish anything yet. LO decides when." (PyPI / the handoff in
  `docs/history/2026-08-23-publishing-handoff.md`.)
- Commit + push diligently, scoped, to `main`.
- Voice: textbook-clear, for a CS junior; no banned phrases; short.
