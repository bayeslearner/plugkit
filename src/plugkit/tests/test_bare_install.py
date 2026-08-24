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
            assert "plugkit[providers]" in str(exc), exc
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
