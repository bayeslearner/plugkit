#!/usr/bin/env python3
"""Render every guide chapter into one self-contained HTML page.

`docs/plugkit-guide.html` is the whole book on one page: masthead, a sticky
chapter rail, every chapter from `docs/guide/*.qmd` in order, and the generated
API reference as an appendix. One file, no assets, no network — it is what a
reader downloads, and what gets published as a shareable page.

It exists as a *script* rather than a checked-in page because the previous
one-page build was hand-made and went stale the day chapter 08 was written: it
still said "eight chapters" and linked `kernel-architecture.md`, a file that had
become `.qmd`. Anything derived from the chapters has to be rebuilt from them.

Build:
    uv run --with markdown --with pygments python scripts/build-guide.py

`markdown` and `pygments` are build-time only. Nothing in `src/plugkit/` imports
them, and `pyproject.toml` still declares `dependencies = []`.
"""
from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import markdown
from pygments.formatters import HtmlFormatter

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "plugkit-guide.html"
SITE = "https://bayeslearner.github.io/plugkit/"

#: Chapters, in reading order. The API reference is appended after these.
ORDER = [
    "00-why.qmd",
    "01-first-plugin.qmd",
    "02-popo-components.qmd",
    "03-tools.qmd",
    "04-your-own-service.qmd",
    "05-config-and-reactivity.qmd",
    "06-composition-from-a-file.qmd",
    "07-testing.qmd",
    "08-supervision.qmd",
]

TITLE = "plugkit — the guide"

LEDE = (
    "Your components are plain classes. plugkit constructs them in dependency "
    "order, passes each one what it asked for, and makes them reachable by name. "
    "When a component unloads, everything it registered is removed — including "
    "registrations it made inside other components."
)

#: Quarto callout kind -> the label printed above the callout title.
CALLOUT_LABEL = {
    "note": "Note",
    "tip": "Tip",
    "important": "Important",
    "warning": "Careful",
    "caution": "Careful",
}

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FIELD = re.compile(r'^(\w+):\s*"?(.*?)"?\s*$', re.MULTILINE)
_CALLOUT = re.compile(
    r"^::: *\{\.callout-(\w+)\}\n(.*?)^::: *$", re.DOTALL | re.MULTILINE
)
_CHAPTER_LINK = re.compile(r"\]\((\d\d)-[\w-]+\.qmd(#[\w-]*)?\)")
_SIBLING_LINK = re.compile(r"\]\((?:\.\./)?((?:design|steering)/[\w-]+)\.(?:qmd|md)\)")
_API_LINK = re.compile(r"\]\((?:\.\./)?api-reference\.qmd\)")
_LOCAL_ANCHOR = re.compile(r"\]\(#([\w-]+)\)")
_PLACEHOLDER = re.compile(r"<p>\x00callout-(\d+)\x00</p>")


@dataclass
class Chapter:
    """One `.qmd` source file, and the identity it gets on the one-page build."""

    slug: str          # "ch-00"
    number: str        # "00", or "A" for the appendix
    title: str
    subtitle: str
    body: str          # markdown, frontmatter stripped

    @classmethod
    def read(cls, path: Path, slug: str, number: str) -> "Chapter":
        text = path.read_text()
        match = _FRONTMATTER.match(text)
        if match is None:
            raise SystemExit(f"{path} has no frontmatter to take a title from")
        fields = dict(_FIELD.findall(match.group(1)))
        return cls(
            slug=slug,
            number=number,
            title=fields.get("title", path.stem),
            subtitle=fields.get("subtitle", ""),
            body=text[match.end():].strip(),
        )

    @property
    def label(self) -> str:
        return "Appendix" if self.number == "A" else f"Chapter {self.number}"


class Renderer:
    """Markdown -> the HTML this page's stylesheet expects.

    Two things Quarto does that plain Markdown does not, and that the chapters
    rely on: `::: {.callout-note}` blocks, and `.qmd` links between chapters.
    Both are resolved here — a chapter link becomes an anchor on this page,
    because on one page there is nowhere else for it to go.

    Heading ids are prefixed per chapter. Every chapter ends in `## Next`, so an
    unprefixed slug would give the page nine elements called `#next` and send
    every one of those links to the first.
    """

    def __init__(self) -> None:
        self.md = markdown.Markdown(
            extensions=["fenced_code", "codehilite", "tables", "toc", "attr_list"],
            extension_configs={
                "codehilite": {"css_class": "codehilite", "guess_lang": False},
            },
        )

    def chapter(self, chapter: Chapter) -> str:
        callouts: list[str] = []
        body = self._lift_callouts(chapter.body, chapter, callouts)
        body = self._rewrite_links(body, chapter)
        rendered = self._render(body, chapter)
        rendered = _PLACEHOLDER.sub(lambda m: callouts[int(m.group(1))], rendered)
        return (
            f'<section class="chapter" id="{chapter.slug}">\n'
            f'  <header class="chapter-head">\n'
            f'    <p class="chapter-num">{html.escape(chapter.label)}</p>\n'
            f"    <h1>{html.escape(chapter.title)}</h1>\n"
            + (
                f'    <p class="chapter-sub">{html.escape(chapter.subtitle)}</p>\n'
                if chapter.subtitle
                else ""
            )
            + "  </header>\n"
            + rendered
            + "\n</section>"
        )

    def _render(self, text: str, chapter: Chapter) -> str:
        self.md.reset()
        # `toc` owns heading ids; give it a per-chapter prefix.
        self.md.treeprocessors["toc"].slugify = lambda value, sep: (
            f"{chapter.slug}-{markdown.extensions.toc.slugify(value, sep)}"
        )
        return self.md.convert(text)

    def _lift_callouts(self, text: str, chapter: Chapter, sink: list[str]) -> str:
        """Replace each callout with a token, rendering its body separately.

        Markdown inside a raw HTML block is not processed, so the callout cannot
        simply be wrapped in `<aside>` before the main render.
        """

        def replace(match: re.Match) -> str:
            kind = match.group(1)
            inner = match.group(2).strip()
            title = ""
            if inner.startswith("## "):
                head, _, rest = inner.partition("\n")
                title = self._inline(head[3:].strip(), chapter)
                inner = rest.strip()
            label = CALLOUT_LABEL.get(kind, kind.title())
            parts = [
                f'<aside class="callout callout-{html.escape(kind)}">',
                f'<p class="callout-kind">{html.escape(label)}</p>',
            ]
            if title:
                parts.append(f'<p class="callout-title">{title}</p>')
            parts.append(self._render(self._rewrite_links(inner, chapter), chapter))
            parts.append("</aside>")
            sink.append("\n".join(parts))
            return f"\x00callout-{len(sink) - 1}\x00"

        return _CALLOUT.sub(replace, text)

    def _inline(self, text: str, chapter: Chapter) -> str:
        """Render one line of markdown without the wrapping `<p>`."""
        rendered = self._render(self._rewrite_links(text, chapter), chapter)
        return re.sub(r"^<p>|</p>$", "", rendered.strip())

    def _rewrite_links(self, text: str, chapter: Chapter) -> str:
        # A same-chapter anchor has to pick up the same prefix the headings got,
        # and this runs first so it cannot re-prefix the `#ch-NN` links below.
        text = _LOCAL_ANCHOR.sub(lambda m: f"](#{chapter.slug}-{m.group(1)})", text)

        def chapter_link(match: re.Match) -> str:
            target = f"ch-{match.group(1)}"
            fragment = match.group(2)
            return f"](#{target}-{fragment[1:]})" if fragment else f"](#{target})"

        text = _CHAPTER_LINK.sub(chapter_link, text)
        text = _API_LINK.sub("](#ch-api)", text)
        return _SIBLING_LINK.sub(lambda m: f"]({SITE}{m.group(1)}.html)", text)


class GuidePage:
    """The whole page: shell, rail, chapters, footer."""

    def __init__(self, chapters: list[Chapter]) -> None:
        self.chapters = chapters
        self.renderer = Renderer()

    def render(self) -> str:
        sections = [self.renderer.chapter(chapter) for chapter in self.chapters]
        return "\n\n".join(
            [
                self._head(),
                self._masthead(),
                self._rail(),
                "  <main>",
                *sections,
                self._footer(),
                "  </main>\n</div>\n" + _THEME_TOGGLE + "\n</body></html>",
            ]
        )

    def _head(self) -> str:
        pygments_css = HtmlFormatter(style="default").get_style_defs(".codehilite")
        page_css = (DOCS / "guide-page.css").read_text()
        return (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n"
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f"<title>{html.escape(TITLE)}</title>\n"
            f"<style>\n{pygments_css}\n{page_css}\n{_TOGGLE_CSS}</style>\n"
            "</head>\n<body>\n<div class=\"wrap\">"
        )

    def _masthead(self) -> str:
        return (
            '  <header class="masthead">\n'
            '    <div class="masthead-inner">\n'
            '      <p class="eyebrow">plugkit · the guide</p>\n'
            "      <h1>A plugin and dependency-injection kernel for Python</h1>\n"
            f'      <p class="lede">{html.escape(LEDE)}</p>\n'
            '      <button class="theme-toggle" type="button" '
            'aria-label="Switch between light and dark">◑ theme</button>\n'
            "    </div>\n  </header>"
        )

    def _rail(self) -> str:
        items = "\n".join(
            f'<li><a href="#{c.slug}"><span class="n">{html.escape(c.number)}</span>'
            f'<span class="t">{html.escape(c.title)}</span></a></li>'
            for c in self.chapters
        )
        return (
            '  <nav class="rail">\n    <p class="rail-label">Chapters</p>\n'
            f"    <ol>{items}</ol>\n  </nav>"
        )

    def _footer(self) -> str:
        count = sum(1 for c in self.chapters if c.number != "A")
        return (
            "    <footer>\n"
            f"      <p>{count} chapters and the API reference, rendered from\n"
            "      <code>docs/guide/*.qmd</code> by <code>scripts/build-guide.py</code>.\n"
            "      The kernel in <code>src/plugkit/cordis/</code> derives from Cordis (MIT)\n"
            "      by way of geohotstan/cordis-py; <code>binding.py</code>, the signals\n"
            "      library, supervision, the tool pipeline and the conformance suite are\n"
            "      plugkit's own.</p>\n"
            f'      <p><a href="{SITE}">{SITE}</a></p>\n'
            "    </footer>"
        )


_TOGGLE_CSS = """
.theme-toggle {
  margin-top: 1.4rem; font-family: ui-sans-serif, system-ui, sans-serif;
  font-size: .78rem; letter-spacing: .04em; color: var(--ink-2);
  background: var(--card); border: 1px solid var(--rule); border-radius: 999px;
  padding: .35rem .85rem; cursor: pointer;
}
.theme-toggle:hover { color: var(--accent); border-color: var(--accent); }
.theme-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""

_THEME_TOGGLE = """<script>
// The stylesheet already answers `prefers-color-scheme`; the button only
// overrides it, by stamping the root element the [data-theme] rules select.
document.querySelector('.theme-toggle').addEventListener('click', () => {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
});
</script>"""


def collect() -> list[Chapter]:
    chapters = [
        Chapter.read(DOCS / "guide" / name, f"ch-{name[:2]}", name[:2]) for name in ORDER
    ]
    chapters.append(Chapter.read(DOCS / "api-reference.qmd", "ch-api", "A"))
    return chapters


def main() -> None:
    chapters = collect()
    page = GuidePage(chapters).render()

    # Self-checks. Each one is a way this page has actually gone wrong before,
    # or would go wrong silently — a stale link renders as ordinary text.
    problems = []
    for chapter in chapters:
        if not chapter.body:
            problems.append(f"{chapter.slug} is empty")
        if f'id="{chapter.slug}"' not in page:
            problems.append(f"{chapter.slug} has no section in the output")
    for href in re.findall(r'href="([^"]+)"', page):
        if href.endswith(".qmd"):
            problems.append(f"link to {href}: a source path a reader cannot open")
        if href.startswith("#") and f'id="{href[1:]}"' not in page:
            problems.append(f"link to {href}, which nothing on the page defines")
    if ":::" in page:
        problems.append("an unclosed callout leaked its fence into the page")
    if problems:
        raise SystemExit("\n".join(f"  - {p}" for p in sorted(set(problems))))

    OUT.write_text(page)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(page):,} bytes, {len(chapters)} sections)")


if __name__ == "__main__":
    sys.exit(main())
