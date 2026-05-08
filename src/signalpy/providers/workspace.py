"""WorkspaceProvider — workspace paths and settings as a component.

Provides: IWorkspace
Requires: IConfig

Gives components a consistent view of the project's filesystem layout:
root directory, settings, and derived paths.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)


@component("workspace", version="0.1")
@provides("IWorkspace")
@requires(config="IConfig")
class WorkspaceProvider:
    """Workspace paths and project-level settings.

    Config keys:
      workspace.root:     project root directory (default: cwd)
      workspace.settings: dict of project-level settings
    """

    @lifecycle.activate
    def activate(self, rt):
        root = rt.config.get("workspace.root", ".")
        self._root = Path(root).resolve()
        self._settings: dict[str, Any] = rt.config.get("workspace.settings", {})

    @property
    def root(self) -> Path:
        """The project root directory."""
        return self._root

    @property
    def settings(self) -> dict[str, Any]:
        """Project-level settings dict."""
        return dict(self._settings)

    def path(self, *parts: str) -> Path:
        """Resolve a path relative to the workspace root."""
        return self._root.joinpath(*parts)

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass
