"""The docs are checked against the code.

`test_readme_examples.py` and `test_guide_examples.py` run what the docs show, so
a documented API that does not exist fails the suite. That leaves everything about
a doc that is not runnable code: counts, paths, links, and whether the snippet on
the page is complete enough to copy.

Those rotted. The conformance-assertion count was stated in five files and wrong in
five, because a number in prose has nothing holding it to the code. This file is
what holds it.

Two doc sets, deliberately different:

- **CURRENT** — README, CLAUDE.md, CHANGELOG, the guide, design and steering docs.
  These say what is true *now*, so a stale number in one is a defect.
- **ARCHIVAL** — `specs/` and `docs/history/`. These say what was true on a date.
  A closed spec recording "32 tests across R1-R6" is a record, not a claim about
  today, so counts there are not policed. Links and paths still are: a record that
  points at a file nobody can open has stopped being a record.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: Top-level directories a doc may name as a repository path. A token like
#: `services/tools.py` or `core/tools` is ambiguous — it could be this repo
#: written loosely, or a path in DeepSeek Harness, which the docs cite often.
#: Only an unambiguously repo-rooted path is checked, so this test never has to
#: guess what a reference means.
REPO_ROOTS = ("src/", "docs/", "specs/", ".github/")

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}

_CODE_SPAN = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_IMPORT = re.compile(r"from\s+plugkit(?:\.\w+)*\s+import\s+([^\n(]+)")


def _python_blocks(text: str) -> list[str]:
    """The fenced Python blocks, paired by scanning rather than by regex.

    A regex pairing ```` ``` ```` with the next ```` ``` ```` desynchronises on the
    first block in another language: the *closing* fence of a ```` ```yaml ```` block
    reads as the opening of a Python one, and every block after it is off by one.
    Chapter 6 opens with YAML, which is how this was found.
    """
    blocks: list[str] = []
    current: list[str] | None = None
    language = ""
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                language = line[3:].strip().lower()
                current = []
            else:
                if language in ("python", "py", ""):
                    blocks.append("\n".join(current))
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


#: Generated, not hand-authored: `guide-one.qmd` concatenates the eight guide
#: chapters into one page. Its internal chapter cross-links (`02-popo-components.qmd`)
#: resolve relative to `guide/` when quarto renders them into the single page, so
#: the link checker would read them as broken repo paths. It has no own truth to
#: hold to the code, so it is not a CURRENT doc.
_GENERATED_DOCS = {"docs/guide-one.qmd"}


def _current_docs() -> list[Path]:
    """Docs that describe the present. A stale count here is a defect."""
    files = [REPO / name for name in ("README.md", "CLAUDE.md", "CHANGELOG.md")]
    for pattern in ("docs/*.qmd", "docs/guide/*.qmd", "docs/design/*", "docs/steering/*"):
        files += sorted(REPO.glob(pattern))
    return [
        f for f in files
        if f.is_file() and f.suffix in (".md", ".qmd") and _rel(f) not in _GENERATED_DOCS
    ]


def _archival_docs() -> list[Path]:
    """Docs that describe a date. Counts are records; links still have to work."""
    return sorted(REPO.glob("specs/**/*.md")) + sorted(REPO.glob("docs/history/**/*.md"))


def _all_docs() -> list[Path]:
    return _current_docs() + _archival_docs()


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def _lines_without_code_spans(path: Path):
    """Yield `(lineno, text)` with inline code blanked out.

    `` `get[T](token: type[T]) -> T` `` is a code span that reads as a markdown
    link. Blanking spans rather than skipping the whole line keeps the rest of it
    checkable.
    """
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        yield number, _CODE_SPAN.sub("", line)


def _conformance_assertion_count() -> int:
    source = (REPO / "src/plugkit/tests/test_conformance.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    return sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    )


def _plugkit_exports() -> set[str]:
    source = (REPO / "src/plugkit/__init__.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") == "__all__" for target in node.targets
        ):
            return {element.value for element in node.value.elts}
    raise AssertionError("plugkit/__init__.py has no __all__")


# ── I3: every relative link resolves ──────────────────────────────────────


def test_every_relative_link_resolves():
    """A link to a file that does not exist is a dead end for the reader."""
    broken = []
    for doc in _all_docs():
        for number, line in _lines_without_code_spans(doc):
            for label, target in _LINK.findall(line):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path = target.split("#", 1)[0]
                if not path:
                    continue
                if not (doc.parent / path).exists():
                    broken.append(f"{_rel(doc)}:{number} [{label}]({target})")
    assert not broken, "links pointing at nothing:\n  " + "\n  ".join(broken)


# ── I4: every repository path named in a doc exists ───────────────────────


def test_every_repository_path_named_in_a_doc_exists():
    """A doc naming `src/plugkit/foo.py` must be edited when foo.py moves."""
    missing = []
    token = re.compile(r"`([^`\n]+)`")
    for doc in _all_docs():
        text = doc.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            for match in token.findall(line):
                candidate = match.split(":", 1)[0].strip()
                if not candidate.startswith(REPO_ROOTS) or " " in candidate:
                    continue
                if "<" in candidate:  # a template: `specs/<NN-name>/spec.md`
                    continue
                found = list(REPO.glob(candidate)) if "*" in candidate else None
                if (found if found is not None else (REPO / candidate).exists()):
                    continue
                missing.append(f"{_rel(doc)}:{number} `{candidate}`")
    assert not missing, "paths named in docs that do not exist:\n  " + "\n  ".join(missing)


# ── I1: stated counts are asserted, or are not stated ─────────────────────


def test_stated_conformance_counts_match_the_suite():
    """The count is the project's central claim, so it is worth stating — checked.

    Archival docs are excluded: a closed spec records what was true at its close.
    """
    actual = _conformance_assertion_count()
    pattern = re.compile(
        r"\b(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\b[^.\n]{0,40}?\bassertions?\b",
        re.IGNORECASE,
    )
    wrong = []
    for doc in _current_docs():
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for raw in pattern.findall(line):
                stated = _NUMBER_WORDS.get(raw.lower(), None)
                if stated is None:
                    if not raw.isdigit():
                        continue
                    stated = int(raw)
                if stated != actual:
                    wrong.append(f"{_rel(doc)}:{number} says {raw!r}, suite has {actual}")
    assert not wrong, (
        "conformance-assertion counts that no longer match "
        f"test_conformance.py ({actual}):\n  " + "\n  ".join(wrong)
    )


def test_no_doc_states_a_whole_suite_test_count():
    """A total that changes on every added test does not belong in prose.

    The conformance count is checked because it is load-bearing and moves rarely.
    A whole-suite total carries nothing a reader needs and would make every new
    test a docs edit, so the rule here is not to state it at all.
    """
    pattern = re.compile(r"\b\d{2,4}\s+(?:passed|passing|tests\b)", re.IGNORECASE)
    found = []
    for doc in _current_docs():
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for hit in pattern.findall(line):
                found.append(f"{_rel(doc)}:{number} {line.strip()!r}")
    assert not found, (
        "a whole-suite test count in prose — say what the suite covers, "
        "not how many:\n  " + "\n  ".join(found)
    )


# ── I7: a diagram on the site actually renders ────────────────────────────


def _rendered_docs() -> list[Path]:
    """The docs Quarto builds into the site, per `docs/_quarto.yml`'s render list.

    Kept as a literal rather than parsed out of the YAML: adding a group to that
    list should be a deliberate edit here too, because a doc that renders is a
    doc whose diagrams and snippets are published.
    """
    files = [REPO / "docs/index.qmd"]
    for pattern in ("docs/guide/*.qmd", "docs/design/*.md", "docs/design/*.qmd",
                    "docs/steering/*.md"):
        files += sorted(REPO.glob(pattern))
    return [f for f in files if f.is_file()]


def test_rendered_docs_use_the_executable_mermaid_fence():
    """Quarto injects its mermaid runtime only for ```{mermaid}, not ```mermaid.

    A plain ```mermaid fence still emits `<pre class="mermaid">`, so the build
    succeeds, the page publishes, and the diagram shows as its own source text
    with no error anywhere. Verified against Quarto 1.7.32: the curly form pulls
    in `mermaid-init.js` and the plain form pulls in nothing.

    This only applies to docs Quarto renders. `specs/` and `docs/history/` are
    read on GitHub, which renders the *plain* form and not the curly one, so the
    two sets want opposite fences and neither is wrong.
    """
    plain = []
    for doc in _rendered_docs():
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() == "```mermaid":
                plain.append(f"{_rel(doc)}:{number}")
    assert not plain, (
        "```mermaid on a rendered page publishes the diagram's source as text. "
        "Use ```{mermaid}:\n  " + "\n  ".join(plain)
    )


# ── I2: a guide page imports what its code blocks name ────────────────────


def _uses_as_a_value(block: str, name: str) -> bool:
    """Whether `name` is used as a value here, not merely mentioned.

    `plugin` is both an export and a context method, so `root.plugin(x)` must not
    count as using the `@plugin` decorator. Every pattern below excludes a
    preceding dot for that reason.
    """
    escaped = re.escape(name)
    patterns = (
        rf"(?<![.\w]){escaped}\s*\(",       # Deny("...")
        rf"(?<![.\w]){escaped}\s*\.",       # Accept.replacing(...)
        rf"@{escaped}\b",                   # @plugin
        rf"\.plugin\(\s*{escaped}\b",       # await root.plugin(PointsService)
        rf"\(\s*{escaped}\s*[,)]",          # ctx.points.add(DIAGNOSTICS, ...)
    )
    return any(re.search(pattern, block) for pattern in patterns)


def test_guide_snippets_import_what_they_name():
    """A chapter's code must be complete enough to copy off the page.

    `test_guide_examples.py` re-types each example into a module that imports
    what it needs at the top, so it proves the API works and cannot prove the page
    does. This is that half.
    """
    exports = _plugkit_exports()
    incomplete = []
    for doc in sorted(REPO.glob("docs/guide/*.qmd")):
        blocks = _python_blocks(doc.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for block in blocks:
            for clause in _IMPORT.findall(block):
                for raw in clause.replace(")", "").split(","):
                    candidate = raw.strip().split(" as ")[0].strip()
                    if candidate.isidentifier():
                        imported.add(candidate)
        used = {
            name
            for name in exports
            if name not in imported
            and any(_uses_as_a_value(block, name) for block in blocks)
        }
        for name in sorted(used):
            incomplete.append(f"{_rel(doc)} uses {name} and never imports it")
    assert not incomplete, (
        "guide code that cannot be run as printed:\n  " + "\n  ".join(incomplete)
    )
