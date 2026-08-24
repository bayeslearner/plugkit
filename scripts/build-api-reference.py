#!/usr/bin/env python3
"""Regenerate `docs/api-reference.qmd` from `plugkit.__all__` and one-line docstrings.

Reading the surface from `__all__` means a public name that a developer forgot
to add to a docstring appears here anyway; a name that loses its docstring
shows up as "no summary". `test_docs_consistency.py` checks the emitted page
against `__all__`, so a stale page fails the suite.

Build: python3 scripts/build-api-reference.py
"""
from pathlib import Path
import inspect
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import plugkit  # noqa: E402

OUT = ROOT / "docs" / "api-reference.qmd"

GROUP = [
    ("The kernel", ["Context", "Service", "Fiber", "FiberState", "Inject", "this_"]),
    ("Binding", ["provide", "plugin", "bind", "snake_case", "CONTEXT_MEMBERS"]),
    ("Signals", ["Signal", "Computed", "Effect", "batch", "is_stale"]),
    (
        "Shipped services",
        [
            "ConfigService", "PointsService", "describe", "format_tree",
            "DIAGNOSTICS", "Contribution", "FileLoader", "load_app",
            "ReactiveService", "SupervisorService", "ToolsService", "timeout_policy",
        ],
    ),
    ("Tool pipeline", ["Tool", "ToolExecution", "ToolResult", "Allow", "Deny", "Ask", "Accept", "Block"]),
    ("Errors", ["CordisError", "ValidationError", "AggregateError"]),
]


#: Module-level constants that are plain values (e.g. a string) have no
#: docstring of their own; these carry their one line here instead.
_CONSTANT_SUMMARY = {
    "DIAGNOSTICS": "the extension point name `describe()` reads for diagnostics",
}


def summary(name: str) -> str:
    if name in _CONSTANT_SUMMARY:
        return _CONSTANT_SUMMARY[name]
    doc = inspect.getdoc(getattr(plugkit, name))
    if not doc:
        return "*no summary — add a docstring*"
    return doc.strip().splitlines()[0]


def rows(names: list[str]) -> str:
    """A pipe table, header row included.

    The header row is not decoration: without it pandoc does not see a table at
    all, and the published page rendered every row as a line of prose with
    literal `|` characters in it for as long as this function omitted it.
    """
    lines = ["| Name | What it is |", "|---|---|"]
    lines += [f"| `{name}` | {summary(name)} |" for name in names]
    return "\n".join(lines)


def main() -> None:
    page = [
        "---",
        'title: "API reference"',
        'subtitle: "Every public name in one line — the signatures the guide uses"',
        "---",
        "",
        "This page is generated from `plugkit.__all__` and each name's one-line",
        "docstring by `scripts/build-api-reference.py`. If you edit it by hand,",
        "the suite will flag the drift.",
        "",
    ]
    for title, names in GROUP:
        missing = [n for n in names if n not in plugkit.__all__]
        if missing:
            raise SystemExit(f"group '{title}' names {missing} not in plugkit.__all__")
        page.append(f"## {title}")
        page.append("")
        page.append(rows(names))
        page.append("")

    # The ungrouped check: every __all__ name is on the page exactly once.
    seen = [n for _, names in GROUP for n in names]
    if sorted(seen) != sorted(plugkit.__all__):
        only_group = set(seen) - set(plugkit.__all__)
        only_all = set(plugkit.__all__) - set(seen)
        raise SystemExit(
            f"page does not match __all__: only-in-group={sorted(only_group)} "
            f"only-in-__all__={sorted(only_all)}"
        )

    OUT.write_text("\n".join(page) + "\n")
    print(f"wrote {OUT} ({len(plugkit.__all__)} names)")


if __name__ == "__main__":
    main()
