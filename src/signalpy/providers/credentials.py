"""CredentialProvider — per-component scoped credential resolver.

Provides: ICredentials
Requires: IConfig

Each component gets credentials scoped to its own config section.
A Splunk component can only see splunk credentials, not GitLab's.
"""
from __future__ import annotations

from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle


@component("credentials", version="0.1")
@provides("ICredentials")
@requires(config="IConfig")
class CredentialProvider:
    """Scoped credential resolver.

    Reads from config at: apps.<component_name>.targets.<target>.<key>
    """

    @lifecycle.activate
    def activate(self, rt):
        self._config = rt.config

    def get(self, key: str, *, target: str | None = None, app: str | None = None) -> str:
        """Resolve a credential value.

        When target is given: apps.<app>.targets.<target>.<key>
        Otherwise: apps.<app>.<key>
        """
        if app is None:
            return ""
        if target:
            val = self._config.get(f"apps.{app}.targets.{target}.{key}")
        else:
            val = self._config.get(f"apps.{app}.{key}")
        return str(val) if val is not None else ""

    def for_target(self, target: str, app: str) -> dict[str, str]:
        """Return all credential key-values for a target."""
        targets = self._config.get(f"apps.{app}.targets.{target}")
        if not isinstance(targets, dict):
            return {}
        return {k: str(v) for k, v in targets.items()}

    def list_targets(self, app: str) -> list[str]:
        """Return sorted target names for an app."""
        targets = self._config.get(f"apps.{app}.targets")
        if not isinstance(targets, dict):
            return []
        return sorted(targets.keys())
