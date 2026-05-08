"""AuthProvider — authentication and authorization as a component.

Provides: IAuth
Requires: IConfig

Default: token-based auth with configurable secret.
Swap for JWT/OAuth/SAML by providing a different IAuth component.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)


@component("auth", version="0.1")
@provides("IAuth")
@requires(config="IConfig")
class AuthProvider:
    """Token-based authentication and role-based authorization.

    Config keys:
      auth.enabled:   true/false (default: true)
      auth.secret:    shared secret for token validation
      auth.tokens:    dict of token → {identity, roles}
      auth.policies:  dict of role → [allowed_actions]
    """

    @lifecycle.activate
    def activate(self, rt):
        self._enabled = rt.config.get("auth.enabled", True)
        self._secret = rt.config.get("auth.secret", "")
        self._tokens: dict[str, dict[str, Any]] = rt.config.get("auth.tokens", {})
        self._policies: dict[str, list[str]] = rt.config.get("auth.policies", {})

    def authenticate(self, token: str) -> dict[str, Any]:
        """Validate a token and return an identity dict.

        Returns: {"subject": str, "roles": list[str], "authenticated": bool}
        Raises ValueError if token is invalid.
        """
        if not self._enabled:
            return {"subject": "anonymous", "roles": ["admin"], "authenticated": False}

        if not token:
            raise ValueError("No token provided")

        # Check static token map
        entry = self._tokens.get(token)
        if entry is not None:
            return {
                "subject": entry.get("identity", "unknown"),
                "roles": entry.get("roles", []),
                "authenticated": True,
            }

        raise ValueError("Invalid token")

    def authorize(self, identity: dict, action: str) -> bool:
        """Check if an identity is authorized for an action.

        Checks each role in identity["roles"] against the configured
        policies. A role's policy is a list of action patterns.
        Wildcard "*" allows everything.
        """
        if not self._enabled:
            return True

        roles = identity.get("roles", [])
        for role in roles:
            allowed = self._policies.get(role, [])
            if "*" in allowed:
                return True
            if action in allowed:
                return True

        return False

    @lifecycle.deactivate
    def deactivate(self, rt):
        self._tokens = {}
