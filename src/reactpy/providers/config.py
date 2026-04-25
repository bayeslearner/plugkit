"""ConfigProvider — layered YAML configuration.

Provides: IConfig
Requires: nothing (leaf provider, activates first)

Config is layered:
  defaults (manifest) → app config.yaml → workspace overlay → env overrides
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from reactpy.kernel import component, provides, lifecycle


@component("config", version="0.1")
@provides("IConfig")
class ConfigProvider:
    """Layered configuration from YAML files.

    On activation, loads config from:
      1. defaults dict (passed as property)
      2. config_path YAML file (if exists)
      3. Environment variable overrides (KERNEL_CFG_<KEY>=value)
    """

    @lifecycle.activate
    def activate(self, rt):
        defaults = rt.properties.get("defaults", {})
        config_path = rt.properties.get("config_path")

        self._data: dict[str, Any] = dict(defaults)

        # Layer 2: YAML file
        if config_path:
            p = Path(config_path)
            if p.is_file():
                with open(p) as f:
                    file_cfg = yaml.safe_load(f) or {}
                self._merge(self._data, file_cfg)

        # Layer 3: Env overrides (KERNEL_CFG_FOO__BAR → foo.bar)
        prefix = "KERNEL_CFG_"
        for key, value in os.environ.items():
            if key.startswith(prefix):
                dotted = key[len(prefix):].lower().replace("__", ".")
                self._set_dotted(dotted, value)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dotted path (e.g. 'apps.splunk.url')."""
        parts = key.split(".")
        cur: Any = self._data
        for part in parts:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part)
            if cur is None:
                return default
        return cur

    def set(self, key: str, value: Any) -> None:
        """Set a config value at runtime (not persisted)."""
        self._set_dotted(key, value)

    def all(self) -> dict[str, Any]:
        """Return the full config dict."""
        return dict(self._data)

    def _set_dotted(self, key: str, value: Any) -> None:
        parts = key.split(".")
        cur = self._data
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value

    @staticmethod
    def _merge(base: dict, overlay: dict) -> None:
        for k, v in overlay.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                ConfigProvider._merge(base[k], v)
            else:
                base[k] = v
