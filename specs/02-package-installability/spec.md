---
spec_id: 02-package-installability
status: CLOSED
closed_as: SHIPPED
since: 2026-08-23
until: null
epic: packaging
features: [bare-install, optional-extras]
supersedes: []
superseded_by: null
depends_on: [01-plugkit-kernel]
anchors: [kernel-architecture]
---

# The package must install and import

# 1 · Requirements

## Introduction

`pyproject.toml` declares `dependencies = []`. The README says the kernel has no
required dependencies and that each shipped service degrades rather than fails
without its extra. Both statements were false: a user who ran `pip install
plugkit` got a package that raised on `import plugkit`.

This spec makes the claim true and adds the test that keeps it true.

## Glossary

- **Bare install** — what `pip install plugkit` produces, with no extras. The
  dependency set is empty.
- **Extra** — an optional dependency group named in
  `[project.optional-dependencies]`, installed with `pip install
  "plugkit[name]"`.
- **Degrade** — the package imports and works, and the one feature needing the
  missing extra raises a message naming the extra. The opposite of failing at
  import.

## The two defects

Both entered by vendoring, and both are import-time, so no amount of runtime
handling reaches them. A module body — including a `class` statement — executes
when the module is imported.

### D1 · `cordis/include.py` imported `yaml` at module level

```console
$ pip install plugkit
$ python -c "import plugkit"
ModuleNotFoundError: No module named 'yaml'
```

`pyyaml` is declared under the `providers` extra. `include.py` used it in three
places: a `yaml.SafeLoader` subclass, two module-level registration calls, and
two methods.

### D2 · `cordis/hmr.py` used an optional class as a base class

```console
$ pip install plugkit          # with pyyaml present, to reach the next failure
$ python -c "import plugkit"
TypeError: NoneType takes no arguments
```

```python
try:
    from watchdog.events import FileSystemEventHandler
except ImportError:
    FileSystemEventHandler = None      # line 32

class _WatchHandler(FileSystemEventHandler):   # line 40 — runs at import
```

The `try/except` guard shows the intent; the class statement defeats it. The
module docstring states the promise it breaks: *"an mtime-polling fallback so the
zero-dependency installation keeps working."*

This is upstream `geohotstan/cordis-py#3`, finding 1, filed 2026-08-23.

## Why the suite could not see either

The development environment installs every extra, so `import plugkit` in a test
always succeeds. A test that imports the package proves nothing about the bare
install. The gap is structural, not an oversight in any one test.

`hmr.py` even carried `# pragma: no cover — exercised by the polling tests`. The
polling tests run with `watchdog` installed, so the guarded branch never ran.

## Requirements

**R1.** `import plugkit` succeeds with no third-party package installed.

**R2.** Every module in the package imports with no third-party package
installed, excluding `cordis/create.py` (a scaffolding CLI, not imported by the
package).

**R3.** Using a feature whose extra is absent raises an error naming the extra
and the install command, not a bare `ModuleNotFoundError`.

**R4.** The suite fails if R1–R3 regress, while running in a development
environment that has every extra installed.

**R5.** CI installs the package bare and imports it, on every supported Python
version.

# 2 · Design

## D1 — resolve `yaml` on first use

Module-level state, imported and configured once, on the first YAML read or
write. The loader subclass and its two registration calls move inside.

```python
_yaml_state: dict = {}

def _yaml():
    """The `yaml` module plus this port's loader, imported on first use."""
    if not _yaml_state:
        try:
            import yaml
        except ImportError as exc:
            raise ImportError(
                "reading or writing a YAML plugin tree needs pyyaml — "
                'install it with `pip install "plugkit[providers]"`'
            ) from exc

        class _CordisYamlLoader(yaml.SafeLoader):
            pass

        _CordisYamlLoader.add_constructor("tag:yaml.org,2002:js", _construct_js)
        yaml.add_representer(JsExpr, _represent_js)
        _yaml_state.update(module=yaml, loader=_CordisYamlLoader)
    return _yaml_state["module"], _yaml_state["loader"]
```

Rejected alternative: declaring `pyyaml` a hard dependency. It is one line, and
it gives up the zero-dependency claim for a feature most users of a DI kernel do
not reach.

## D2 — bind a base class that always exists

```python
# A class body runs at import, so `class _WatchHandler(None)` would make
# `import plugkit` fail on the bare install rather than fall back to polling.
_WatchBase = FileSystemEventHandler if WATCHDOG_AVAILABLE else object

class _WatchHandler(_WatchBase):
```

`object` is correct rather than a stand-in: without `watchdog` there is no
observer to hand the handler to, and `Hmr` already branches on
`WATCHDOG_AVAILABLE` before constructing one. The class exists so the module is
importable and polling mode is reachable.

## The test — hide the extras in a subprocess

A `sys.meta_path` finder that raises `ImportError` for named top-level packages,
injected into a child process before the package is imported. This reproduces the
bare install without building a venv, and runs in the same suite that has the
extras installed.

```python
class _Blocked:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _BLOCKED:
            raise ImportError("no module named " + fullname + " (blocked by the test)")
        return None

sys.meta_path.insert(0, _Blocked())
```

`src/plugkit/tests/test_bare_install.py`:

| Test | Asserts |
|---|---|
| `test_import_plugkit_without_any_optional_package` | R1 |
| `test_every_submodule_imports_without_any_optional_package` | R2, by walking `pkgutil.walk_packages` |
| `test_import_succeeds_with_each_extra_missing_individually` | R1 per extra, so one missing extra cannot mask another |
| `test_reading_yaml_without_pyyaml_names_the_extra` | R3 |
| `test_hmr_falls_back_to_polling_without_watchdog` | R3, and that `_WatchHandler` is still a class |
| `test_the_walk_actually_reaches_the_modules_that_broke` | that the walk in R2 reaches `include` and `hmr` — a walk that skipped them would pass regardless |

The last one guards the guard. A regression test whose search silently misses the
file it was written for is worse than no test, because it reads as coverage.

# 3 · Tasks

- [x] **T1** — resolve `yaml` on first use in `cordis/include.py`; both call
  sites updated (`read`, `_write_file`)
- [x] **T2** — bind `_WatchBase` in `cordis/hmr.py`
- [x] **T3** — write `tests/test_bare_install.py`, seven tests
- [x] **T4** — verify the tests fail on the original defects: both reintroduced,
  6 of 7 failed; restored, 7 of 7 passed
- [x] **T5** — verify a real bare install in a clean venv:
  `uv pip install .` then `import plugkit`, `Context()`, and
  `WATCHDOG_AVAILABLE is False`
- [x] **T6** — CI job `bare-install` installing the wheel with nothing else and importing every module, on 3.13 and 3.14; the mis-named `no-optional-deps` job that installed the extras it claimed to omit is renamed `without-dependency-injector` and now says what it does
- [x] **T7** — record the rule in `CLAUDE.md` so it survives into later sprints

## Verification

```console
$ uv run pytest src/plugkit/tests -q
303 passed, 3 skipped, 2 xfailed

$ uv venv /tmp/bare && uv pip install --python /tmp/bare/bin/python .
$ /tmp/bare/bin/python -c "import plugkit; plugkit.Context()"
import OK 0.1.0
Context OK
watchdog: False
```

## Notes

A module-level third-party import is the only defect class this spec closes.
Neither the type checker nor the suite can see it from inside an environment that
has the package installed, which is why the check has to hide it and why the
check belongs in CI rather than in a reviewer's head.
