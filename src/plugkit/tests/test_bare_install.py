"""The zero-dependency install must import.

`pyproject.toml` declares `dependencies = []`. Every third-party package is an
extra, and the README says each shipped service degrades rather than fails
without its extra. That promise is about *import time*: a module-level
`import yaml`, or a class whose base comes from an optional package, breaks
`import plugkit` for anyone who ran a plain `pip install plugkit`.

The development environment has every extra installed, so a plain import in the
suite cannot see the regression. These tests hide the optional packages in a
subprocess and import the package as a bare user would.

Both failures this guards against were real, and both came in by vendoring:

    include.py:10   import yaml                     -> ModuleNotFoundError
    hmr.py:40       class _WatchHandler(None)       -> TypeError

The second is upstream geohotstan/cordis-py#3, finding 1.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys
import textwrap

import pytest

OPTIONAL_PACKAGES = ["yaml", "watchdog"]

# Refuses the named top-level packages, so the child process sees the
# dependency set of a plain `pip install plugkit`.
_BLOCKER = """
import sys

_BLOCKED = __NAMES__

class _Blocked:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in _BLOCKED:
            raise ImportError("no module named " + fullname + " (blocked by the test)")
        return None

sys.meta_path.insert(0, _Blocked())
for name in list(sys.modules):
    if name.split(".")[0] in _BLOCKED:
        del sys.modules[name]
"""


def _run_without(packages: list[str], body: str) -> subprocess.CompletedProcess:
    script = _BLOCKER.replace("__NAMES__", repr(packages)) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_import_plugkit_without_any_optional_package():
    result = _run_without(
        OPTIONAL_PACKAGES,
        """
        import plugkit
        root = plugkit.Context()
        assert root is not None
        print("OK")
        """,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_every_submodule_imports_without_any_optional_package():
    """A module-level third-party import anywhere in the tree fails here."""
    result = _run_without(
        OPTIONAL_PACKAGES,
        """
        import importlib
        import pkgutil

        import plugkit

        failed = []
        for info in pkgutil.walk_packages(plugkit.__path__, "plugkit."):
            if ".tests" in info.name or info.name.endswith(".create"):
                continue
            try:
                importlib.import_module(info.name)
            except ImportError as exc:
                failed.append(f"{info.name}: {exc}")
        print("FAILED:" + repr(failed))
        assert not failed, failed
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("missing", OPTIONAL_PACKAGES)
def test_import_succeeds_with_each_extra_missing_individually(missing: str):
    result = _run_without(
        [missing],
        """
        import plugkit
        plugkit.Context()
        print("OK")
        """,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_reading_yaml_without_pyyaml_names_the_extra():
    """Degrading means a message that says what to install, not a bare crash."""
    result = _run_without(
        ["yaml"],
        """
        from plugkit.cordis.include import _yaml

        try:
            _yaml()
        except ImportError as exc:
            assert "pyyaml" in str(exc), exc
            assert "plugkit[yaml]" in str(exc), exc
            print("OK")
        else:
            raise AssertionError("expected ImportError")
        """,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_hmr_falls_back_to_polling_without_watchdog():
    result = _run_without(
        ["watchdog"],
        """
        import plugkit.cordis.hmr as hmr

        assert hmr.WATCHDOG_AVAILABLE is False
        # the class still exists, so the module is usable in polling mode
        assert isinstance(hmr._WatchHandler, type)
        print("OK")
        """,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_the_walk_actually_reaches_the_modules_that_broke():
    """Guards the guard: a walk that silently skipped these would pass anyway."""
    import plugkit

    found = {
        info.name
        for info in pkgutil.walk_packages(plugkit.__path__, "plugkit.")
        if ".tests" not in info.name
    }
    assert "plugkit.cordis.include" in found
    assert "plugkit.cordis.hmr" in found


# ── every extra installs something the package imports ───────────────────


#: Distribution name -> the module it provides, where they differ.
_MODULE = {
    "pyyaml": "yaml",
    "dependency-injector": "dependency_injector",
}

#: Extras that carry test tooling rather than package dependencies.
_TOOLING = {"dev"}


def test_no_phantom_extras():
    """An extra must install something `src/plugkit` actually imports.

    `plugkit[rest]` used to install fastapi and uvicorn, `plugkit[cli]` click,
    `plugkit[tracing]` three opentelemetry packages, and `plugkit[mcp]` nothing
    at all — while no module imported any of them. Extras are public API: a
    consumer who reads `[rest]` and installs it has been told this package does
    something with a web framework, and it does not.
    """
    import re
    import tomllib
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    config = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    extras = config["project"].get("optional-dependencies", {})

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (repo / "src/plugkit").rglob("*.py")
        if "tests" not in path.parts
    )

    phantom = []
    for extra, requirements in extras.items():
        if extra in _TOOLING:
            continue
        if not requirements:
            phantom.append(f"{extra}: installs nothing")
            continue
        for requirement in requirements:
            distribution = re.split(r"[<>=!\[ ]", requirement, maxsplit=1)[0]
            if distribution == "plugkit":  # the `all` aggregate
                continue
            module = _MODULE.get(distribution, distribution.replace("-", "_"))
            if not re.search(rf"^\s*(?:import|from)\s+{re.escape(module)}\b", sources, re.M):
                phantom.append(f"{extra}: nothing imports {module} ({distribution})")

    assert not phantom, (
        "extras with nothing behind them:\n  " + "\n  ".join(phantom)
    )


def test_declared_python_matches_the_classifiers():
    """PyPI renders the classifiers; pip enforces `requires-python`.

    They said different things at the first upload: `>=3.13` with classifiers
    advertising 3.11 and 3.12, so the project page offered interpreters that
    `pip install` then refused.
    """
    import tomllib
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    floor = tuple(int(part) for part in project["requires-python"].lstrip(">=").split("."))
    advertised = [
        tuple(int(part) for part in line.rsplit(" :: ", 1)[1].split("."))
        for line in project["classifiers"]
        if line.startswith("Programming Language :: Python :: 3.")
    ]
    below = [".".join(map(str, version)) for version in advertised if version < floor]
    assert not below, (
        f"classifiers advertise {below}, which requires-python "
        f"{project['requires-python']!r} refuses to install on"
    )


def test_the_version_is_stated_once():
    """`plugkit.__version__` and the installed metadata cannot disagree.

    They did, for two releases: the module said 0.1.0 while the distribution
    said 0.3.0, because the string was hand-maintained beside a `version =` in
    `pyproject.toml` that the bumps updated instead. Anyone gating a feature on
    `__version__` — "does this plugkit have the watcher API?" — got the wrong
    answer. `pyproject.toml` now reads the version from the module, so this test
    is checking that the wiring is still in place rather than that a human
    remembered.
    """
    import importlib.metadata as metadata
    import tomllib
    from pathlib import Path

    import plugkit

    repo = Path(__file__).resolve().parents[3]
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "version" in project.get("dynamic", []), (
        "pyproject.toml pins a literal version again; it must read the module's"
    )

    try:
        installed = metadata.version("plugkit")
    except metadata.PackageNotFoundError:  # a source tree that was never installed
        installed = None
    if installed is not None:
        assert plugkit.__version__ == installed, (
            f"plugkit.__version__ is {plugkit.__version__}, the installed "
            f"distribution is {installed}"
        )

    latest = next(
        line for line in (repo / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("## [")
    )
    assert plugkit.__version__ in latest, (
        f"the changelog's newest section is {latest!r}, "
        f"but the package is {plugkit.__version__}"
    )
