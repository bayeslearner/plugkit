# Pillars — what this project lives or dies on

The dimensions on which a kernel succeeds. Code is one of five and usually the
least at risk.

Every sprint advances at least one. A sprint that advances only **Kernel** and
leaves Teaching and Design untouched is a sprint that made the project harder to
adopt.

| Pillar | What it means here | Healthy when |
|---|---|---|
| **Kernel** | The plugin machinery itself: context, fiber, effects, events, registry, composition | A plugin loads when its dependencies appear, unloads totally when they go, and hot reload falls out of that rather than being a feature |
| **Adoptability** | Whether a stranger can put a component into this without adopting a religion | A component is a plain class. It imports nothing from the kernel, is constructible in a test with no fixtures, and works outside this framework unchanged |
| **Conformance** | Whether "this implements Cordis" is a claim or a fact | `test_conformance.py` passes, every assertion traced to `vendor/cordis/src/*.ts`. DeepSeek Harness's documentation stays a usable specification for anything built here |
| **Teaching** | Whether the Quarto book explains the model, not just the API | A newcomer can read the guide end to end and write a correct plugin without reading kernel source. Every concept has a runnable example |
| **Packaging** | Whether it can be depended on | `pip install` works, extras are honest about what degrades without them, the vendored fork's provenance and patch list are current |

## Current state — 2026-08-24

| Pillar | State | Evidence |
|---|---|---|
| **Kernel** | 🟢 strong | The vendored suite, 17 conformance assertions, and one file per feature, all green [src:src/plugkit/tests/] |
| **Adoptability** | 🟢 strong | `binding.provide()` keeps components POPO; `test_component_needs_no_kernel_at_all` is the guard [src:src/plugkit/tests/test_binding.py] |
| **Conformance** | 🟡 partial | 17 assertions cover the load-bearing semantics. Of DeepSeek Harness's 58 service keys, one is implemented (`ctx.tools`) [src:src/plugkit/services/tools.py] |
| **Teaching** | 🟡 partial | The guide runs from why it exists to testing, every example executed by `test_guide_examples.py` and `test_readme_examples.py`, and `test_docs_consistency.py` holds each page to importing what it names [src:src/plugkit/tests/test_docs_consistency.py]. `ctx.supervisor` ships with no chapter, and there is no API reference page |
| **Packaging** | 🟡 partial | One package, `plugkit` 0.1.0, with honest `config`/`hmr` extras. Unreleased; the PyPI name is claimed but nothing is published. The vendored kernel is a fork, not a dependency [src:src/plugkit/VENDORED.md] |

## The honest read

**Packaging is the live risk.** The name is claimed, nothing is published, and
until `pip install plugkit` works the adoptability argument is theoretical. The
build, the workflow and the trusted-publishing binding are ready; the remaining
steps and the one decision left are written down in `docs/history/`.

**Teaching moved from strong to partial, on measurement rather than on new
damage.** Reading every page against the code found a shipped service with no
chapter and thirteen snippets that could not be run as printed. The snippets are
fixed and now checked; the missing chapter is a sprint of its own.

**Conformance is a ceiling, not a gap.** The assertion count is the right number
for what exists. It grows as services are added, not before, and
`test_docs_consistency.py` fails when a doc states a number the suite no longer
holds.

**Scope is deliberately narrow.** plugkit is a kernel plus five services. It has
no auth, credentials, storage, tracing or transport layer, and adding them is not
the plan — those belong to whatever is built on top.
`docs/design/what-it-does-not-replace.qmd` states this against
`dependency-injector`, `pluggy` and `iPOPO`. Any claim that plugkit supersedes
them is false and must not appear in the docs.
