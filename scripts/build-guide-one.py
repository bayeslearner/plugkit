#!/usr/bin/env python3
"""Concatenate the 8 guide chapters into one .qmd.

Rendering `docs/guide-one.qmd` with quarto makes a single self-contained
bright HTML page (`guide-one.html`) combining every chapter, with the mermaid
diagram and runnable code blocks. That page is what a reader downloads to
read the whole guide offline in one file.

Build:  python3 scripts/build-guide-one.py
Render: cd docs && quarto render guide-one.qmd   (writes guide-one.html)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide"
OUT = ROOT / "docs" / "guide-one.qmd"

ORDER = [
    "00-why.qmd",
    "01-first-plugin.qmd",
    "02-popo-components.qmd",
    "03-tools.qmd",
    "04-your-own-service.qmd",
    "05-config-and-reactivity.qmd",
    "06-composition-from-a-file.qmd",
    "07-testing.qmd",
]

FRONTMATTER = """---
title: "plugkit — the whole guide"
subtitle: "All eight chapters, one bright page"
format:
  html:
    theme:
      light: [cosmo, theme-light.scss]
      dark: [darkly, theme-dark.scss]
    css: theme-neutral.css
    code-copy: true
    code-overflow: wrap
    toc: true
    toc-depth: 3
    embed-resources: true
    mermaid:
      theme: neutral
---
"""


def chapter_body(path: Path) -> str:
    text = path.read_text()
    if text.lstrip().startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def main() -> None:
    bodies = [chapter_body(GUIDE / name) for name in ORDER]
    content = FRONTMATTER + "\n\n" + "\n\n".join(bodies) + "\n"
    OUT.write_text(content)

    # Self-check: the page must hold all eight chapters and the bright theme.
    for name in ORDER:
        if name not in content:
            raise SystemExit(f"missing chapter {name}")
    if "theme-light.scss" not in FRONTMATTER:
        raise SystemExit("bright theme not wired")
    print(f"wrote {OUT} ({len(content)} chars, {len(bodies)} chapters)")


if __name__ == "__main__":
    main()
