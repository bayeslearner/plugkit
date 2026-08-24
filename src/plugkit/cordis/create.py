"""create-cordis — scaffold a new Python cordis project.

Like the JS package, templates are fetched from the package registry (PyPI's
JSON API) and extracted into the target directory; `-t` selects the template
package, `-r` pins a version, and `-m` overrides the registry mirror.  Without
`-t`, a bundled template is scaffolded so the CLI works offline.
"""

from __future__ import annotations

import argparse
import configparser
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REGISTRY = "https://pypi.org/pypi"

TEMPLATE = {
    "pyproject.toml": """[project]
name = "__NAME__"
version = "0.1.0"
description = "A cordis application"
requires-python = ">=3.11"
dependencies = ["plugkit"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["."]
""",
    "main.py": '''"""Entry point of __NAME__."""

import asyncio

from plugkit.cordis import Context


async def main():
    ctx = Context()

    await ctx.plugin(lambda ctx, config: print("hello,", config["name"]), {"name": "__NAME__"})

    ctx.emit("ready")


if __name__ == "__main__":
    asyncio.run(main())
''',
    "README.md": """# __NAME__

A Python [cordis](https://github.com/cordiverse/cordis) application.

```bash
uv sync
uv run python main.py
```
""",
    "cordis.yml": """- id: main
  name: ./main
""",
}


def resolve_registry(mirror: str | None = None) -> str:
    """The JS package asks npm for the registry; the Python port reads
    `--mirror`, `PIP_INDEX_URL`, and `~/.pypirc` in order."""
    if mirror:
        return mirror.rstrip("/")
    for env in ("PIP_INDEX_URL", "CORDIS_REGISTRY"):
        value = os.environ.get(env)
        if value:
            return value.rstrip("/")
    pypirc = os.path.expanduser("~/.pypirc")
    if os.path.exists(pypirc):
        try:
            config = configparser.ConfigParser()
            config.read(pypirc)
            for section in ("global", "pypi"):
                if config.has_option(section, "index-url"):
                    return config.get(section, "index-url").rstrip("/")
        except (configparser.Error, OSError):
            pass
    return DEFAULT_REGISTRY


def fetch_package_meta(registry: str, name: str) -> dict:
    url = f"{registry}/{name}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def resolve_version(meta: dict, ref: str | None) -> str:
    if ref in (None, "", "latest"):
        return meta["info"]["version"]
    if ref in (meta.get("releases") or {}):
        return ref
    raise Exception(f'unknown template version "{ref}"')


def _file_candidates(meta: dict, version: str) -> list:
    files = (meta.get("releases") or {}).get(version) or meta.get("urls") or []
    return [f for f in files if f.get("packagetype") in ("sdist", "bdist_wheel")]


def download_template(registry: str, meta: dict, version: str, target: Path) -> None:
    """Download the template distribution and extract it (strip the top-level
    directory, like `tar --strip-components=1`)."""
    candidates = _file_candidates(meta, version)
    candidate = next((f for f in candidates if f.get("packagetype") == "sdist"), None)
    if candidate is None and candidates:
        candidate = candidates[0]
    if candidate is None:
        raise Exception(f'template "{meta["info"]["name"]}" has no downloadable files')

    url = candidate["url"].split("#", 1)[0]
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()

    filename = url.rsplit("/", 1)[-1]
    target.mkdir(parents=True, exist_ok=True)
    if filename.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2:
                    continue
                member.name = parts[1]
                if member.isdir():
                    (target / member.name).mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    (target / member.name).parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as source:
                        (target / member.name).write_bytes(source.read())
    else:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                parts = info.filename.split("/", 1)
                if len(parts) < 2 or parts[1].endswith("/"):
                    continue
                name = parts[1]
                if ".dist-info/" in name:
                    continue
                (target / name).parent.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(archive.read(info))


def _replace_name(target: Path, name: str) -> None:
    """JS `writePackageJson()` sets the project name; the Python equivalent
    rewrites `__NAME__` placeholders or the `[project]` name field."""
    for path in target.rglob("*"):
        if not path.is_file() or path.name.endswith((".pyc", ".dist-info")):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "__NAME__" in content:
            path.write_text(content.replace("__NAME__", name), encoding="utf-8")
    pyproject = target / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        lines = content.splitlines()
        in_project = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_project = stripped == "[project]"
                continue
            if in_project and stripped.startswith("name") and "=" in stripped:
                key = line.split("=", 1)[0]
                lines[index] = f'{key}= "{name}"'
                break
        pyproject.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _strip_dev_tables(text: str) -> str:
    """JS `--prod` removes devDependencies/workspaces; the Python analogue
    strips `[dependency-groups]` and its sub-tables from pyproject.toml."""
    lines = text.splitlines()
    result = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            skip = stripped == "[dependency-groups]" or stripped.startswith("[dependency-groups.")
        if not skip:
            result.append(line)
    return "\n".join(result) + "\n"


def _supports(command: str) -> bool:
    try:
        subprocess.run([command, "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _prompt_yes(message: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(message + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _prompt_text(message: str, default: str) -> str:
    answer = input(f"{message} ({default}): ").strip()
    return answer or default


def scaffold(
    name: str,
    directory: str,
    force: bool = False,
    git: bool = False,
    yes: bool = False,
    template: str | None = None,
    ref: str | None = None,
    mirror: str | None = None,
    prod: bool = False,
) -> str:
    target = Path(directory)
    if target.exists() and any(target.iterdir()):
        if not force and not yes:
            print(f'  Target directory "{directory}" is not empty.')
            if not _prompt_yes("  Remove existing files and continue?"):
                sys.exit(0)
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    target.mkdir(parents=True, exist_ok=True)

    if template:
        registry = resolve_registry(mirror)
        print("  Registry server: " + registry)
        print(f"  Scaffolding project in {directory} ...")
        meta = fetch_package_meta(registry, template)
        version = resolve_version(meta, ref)
        print(f"  Template {template}@{version}")
        download_template(registry, meta, version, target)
        _replace_name(target, name)
        if prod:
            pyproject = target / "pyproject.toml"
            if pyproject.exists():
                pyproject.write_text(_strip_dev_tables(pyproject.read_text(encoding="utf-8")), encoding="utf-8")
    else:
        for filename, content in TEMPLATE.items():
            (target / filename).write_text(content.replace("__NAME__", name), encoding="utf-8")

    print(f"  Done. Scaffolded {name} in {directory}")

    if git:
        if _supports("git"):
            subprocess.run(["git", "init"], cwd=target, capture_output=True)
            print("  Initialized a git repository.")
        else:
            print("  git not found; skipped repository initialization.")

    return str(target)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cordis-create", description="scaffold a new cordis project")
    parser.add_argument("name", nargs="?", help="project name (defaults to cordis-app)")
    parser.add_argument("-t", "--template", help="template package on the registry (default: bundled template)")
    parser.add_argument("-r", "--ref", help="template version (default: latest)")
    parser.add_argument("-m", "--mirror", help="registry mirror URL")
    parser.add_argument("-f", "--forced", "--force", action="store_true", help="overwrite non-empty directories")
    parser.add_argument("-g", "--git", action="store_true", help="initialize a git repository")
    parser.add_argument("-p", "--prod", action="store_true", help="strip development dependencies")
    parser.add_argument("-y", "--yes", action="store_true", help="skip prompts")
    args = parser.parse_args(argv)

    name = args.name or ("cordis-app" if not sys.stdin.isatty() or args.yes else _prompt_text("Project name", "cordis-app"))
    target = os.path.join(os.getcwd(), name)
    scaffold(
        name,
        target,
        force=args.forced,
        git=args.git,
        yes=args.yes,
        template=args.template,
        ref=args.ref,
        mirror=args.mirror,
        prod=args.prod,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
