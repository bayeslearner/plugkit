"""Verify the typed-`ctx` claims instead of asserting them in prose.

Skipped when pyright is not installed, so the normal suite stays fast and
dependency-free. Run it with:

    uv run --with pyright --with pytest pytest src/plugkit/tests/test_typing.py
"""

import json
import shutil
import subprocess
import textwrap

import pytest

from plugkit.examples.typed_plugin import ReporterDeps, reporter

pyright = pytest.mark.skipif(
    shutil.which("pyright") is None, reason="pyright not installed"
)


def _check(source: str) -> list[dict]:
    """Run pyright over a snippet, return its diagnostics."""
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snippet.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(source))
        proc = subprocess.run(
            ["pyright", "--outputjson", path], capture_output=True, text=True
        )
        return json.loads(proc.stdout)["generalDiagnostics"]


def test_protocol_derives_the_inject_list_at_runtime():
    """No pyright needed: the Protocol and the inject list cannot drift."""
    assert reporter["inject"] == ["config", "database"]
    from typing import get_protocol_members

    assert get_protocol_members(ReporterDeps) == {"database"}


@pyright
def test_a_raw_context_does_not_satisfy_an_arbitrary_protocol():
    """__getattr__ -> Any is not enough. This is why you type the parameter."""
    diagnostics = _check(
        """
        from typing import Any, Protocol

        class Ctx:
            def __getattr__(self, name: str) -> Any: ...

        class HasTools(Protocol):
            tools: object

        def wants(ctx: HasTools) -> None: ...
        wants(Ctx())
        """
    )
    errors = [d for d in diagnostics if d["severity"] == "error"]
    assert errors, "pyright accepted a raw Context where a Protocol was required"


@pyright
def test_annotating_the_parameter_types_it_and_catches_typos():
    diagnostics = _check(
        """
        from typing import Any, Protocol

        class Tools:
            def register(self, name: str) -> int: ...

        class Deps(Protocol):
            tools: Tools

        def plugin(ctx: Deps, config: Any = None) -> None:
            ctx.tools.register("ok")
            ctx.tools.regsiter("typo")
        """
    )
    errors = [d for d in diagnostics if d["severity"] == "error"]
    assert len(errors) == 1, [d["message"] for d in errors]
    assert "regsiter" in errors[0]["message"]


@pyright
def test_the_shipped_example_typechecks():
    import plugkit.examples.typed_plugin as module

    proc = subprocess.run(
        ["pyright", "--outputjson", module.__file__], capture_output=True, text=True
    )
    report = json.loads(proc.stdout)
    errors = [d for d in report["generalDiagnostics"] if d["severity"] == "error"]
    assert not errors, [d["message"] for d in errors]
