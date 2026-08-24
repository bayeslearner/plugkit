# plugkit

A plugin kernel for Python. **Every registration returns its undo, and something
owns it.**

## What this is

A component adds a route to a shared server, then unloads. The route stays,
because nothing recorded who added it. That is a missing invariant, not a missing
feature — so this kernel has one: the **fiber** (the object representing a
plugin's lifetime) holds every disposer that plugin produced, and calls them on
unload.

Hot reload, dependency-driven activation, and safe implementation swapping are
consequences of that, not separate features.

Four concepts: **context** (a lookup table of services), **service** (something
registered under a name), **plugin** (a callable that gets a context and
registers things), **fiber** (one mounted plugin and everything it registered).

**The kernel in `cordis/` derives from Cordis** (MIT), the plugin framework
underneath DeepSeek Harness, by way of `geohotstan/cordis-py`. Roughly two thirds
of the source lines. What is plugkit's own: `binding.py`, Protocol-driven
injection, `signals.py`, supervision, the tool pipeline, the loader, subscript
access, and the conformance suite. Docs lead with what the project does and keep
provenance in its own section — do not let them drift back to leading with it,
and do not let them claim more than the split above.

Matching Cordis's semantics is practical rather than decorative: it keeps dsh's
documentation a working specification for anything built here.

## Skills to load each session

`work-discipline` and `spec-driven-dev`. Add `deepseek-harness` when working on
anything that mirrors a dsh subsystem.

## Required reading (session start)

1. `docs/steering/pillars.md` — what this project lives or dies on
2. `docs/design/kernel-architecture.qmd` — the anchor every sprint conforms to
3. The head of the sprint queue in `specs/` — the lowest-numbered spec that is
   not CLOSED. That is where work is.

## Where truth lives

| kind | location |
|---|---|
| sprints | `specs/<NN-name>/spec.md` |
| pillars, long-lived intent | `docs/steering/` |
| stable cross-cutting design (the anchors) | `docs/design/` |
| the user-facing book | `docs/guide/`, `docs/index.qmd` |
| dated, time-bound records | `docs/history/` |
| what was vendored, why, and every change made to it | `src/plugkit/VENDORED.md` |

## Layout

Four levels, ordered by what may depend on what.

```
src/plugkit/
  cordis/       the kernel — nothing above it may be assumed present
  binding.py    provide() and @plugin — the kernel-aware wiring layer
  signals.py    Signal / Computed / Effect — a standalone library, imports nothing
  services/     ordinary plugins with NO privileged status:
                  config, reactive, supervision, tools
  examples/     typed_plugin.py, alternative_binding.py
  tests/        vendored suite + conformance + one file per feature
```

**There is no platform tier.** Config and logging are ordinary plugins, not
system services. The kernel's only built-in service is `ctx.logger`, and it earns
that because a fiber must be able to report its own load failure.

**Rule: nothing in `cordis/` may import from `services/`.** That direction would
make a service load-bearing, which is the tier this design deleted.

## Commands

Run the tests and add dependencies with `uv`:

```bash
uv run --extra dev pytest src/plugkit/tests -q      # the gate
uv run --extra dev --with pyright pytest src/plugkit/tests/test_typing.py
uv add <pkg>                                        # never pip/poetry
```

`--extra dev` is not optional. It carries pytest itself, plus `pyyaml`, without
which the loader, include and config-YAML tests fail — fifteen of them. It is the
same set CI installs (`pip install -e ".[dev]"` in `.github/workflows/test.yml`),
so the local gate and the CI gate are the same gate.

Test config (`asyncio_mode`, `pythonpath`) is in `pyproject.toml`. Optional
extras: `config` (dependency-injector), `hmr` (watchdog). Both degrade rather
than fail, and the suite passes with and without them — `without-dependency-injector`
and `bare-install` in the CI workflow are the jobs that hold that claim up.

## Working rules

**`test_conformance.py` is the gate.** 17 assertions traced to
`vendor/cordis/src/*.ts` rather than to this implementation. Run it first when
taking any upstream change. It is what decided which of the three public Cordis
ports to vendor.

**`test_bare_install.py` is the release gate.** `pyproject.toml` declares
`dependencies = []`, so a module-level import of a third-party package breaks
`import plugkit` for everyone who ran a plain `pip install`. The dev environment
has every extra installed and cannot see it; that test hides them in a subprocess.
Two such bugs shipped in the vendored kernel before it existed.

**Components stay plain objects.** A class the kernel wires must import nothing
from `plugkit` and must be constructible in a test with no fixtures.
`test_component_needs_no_kernel_at_all` is the guard. `Service` is for plugins
that *are* kernel surface — the shipped services. Application code uses
`provide()`.

**A test asserts the property a feature exists to provide**, not its API
surface. "A subscription dies with the plugin that made it" is a test; "effect()
returns an object" is not.

**Watch for the callable-attribute hazard.** Reading a service through a context
rebinds its *methods* onto the caller's view, so registrations are owned by the
caller's fiber. It must not touch data that happens to be callable — a provider,
a lambda, a partial. Got this wrong twice; the discriminator is whether the name
lives in the instance `__dict__`. See `VENDORED.md`.

**Simplicity over compatibility.** No shims, no dual paths. When behaviour
changes, replace rather than accrete.
