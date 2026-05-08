"""Example 08 — Secret Rotation

A database client whose API key is rotated by an external vault.
When the credential changes in config, the @effect re-runs and
the client reconnects with the new key — automatically.

Domain: credential management / vault integration.
Shows: Signal-backed config, @effect, credential propagation,
       CredentialProvider as reactive pass-through.

The chain: vault calls config.set() → ConfigProvider's internal Signal
notifies → consumer's @effect (which reads credentials → which reads
config) re-runs → client reconnects with new key.

No kernel changes required. The reactive graph handles it.

Run: PYTHONPATH=src python -m signalpy.examples.08_secret_rotation
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle, effect,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider
from signalpy.providers.credentials import CredentialProvider


# ── The database client — reconnects when credentials change ────────

@component("db-client", version="1.0")
@requires(config="IConfig", creds="ICredentials")
class DatabaseClient:
    """Simulated database client that uses an API key from credentials.

    The @effect reads self.rt.creds which ultimately reads config.
    When config changes (secret rotation), the effect re-runs and
    the client "reconnects" with the new key.
    """

    @lifecycle.activate
    def activate(self):
        self.connection_log = []
        self._connected_key = None

    @effect
    def on_credential_change(self):
        """Reconnect when credentials change."""
        key = self.rt.creds.get("api_key", target="prod")
        if key and key != self._connected_key:
            self._connected_key = key
            masked = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
            self.connection_log.append(f"connected:{masked}")
            print(f"  [effect] DB client reconnected with key {masked}")

    @runnable("status", params=BaseModel, description="Current connection status")
    async def status(self, params):
        return {
            "connected_key_prefix": self._connected_key[:4] + "..." if self._connected_key else None,
            "reconnect_count": len(self.connection_log),
        }


# ── The vault — rotates secrets by updating config ──────────────────

@component("vault", version="1.0")
@requires(config="IConfig")
class VaultSimulator:
    """Simulates a vault that rotates API keys.

    In production, this would poll HashiCorp Vault / AWS Secrets Manager
    and push new values into config. Here we just call config.set().
    """

    @lifecycle.activate
    def activate(self):
        self._rotation_count = 0

    @runnable("rotate", params=BaseModel, description="Rotate the DB API key")
    async def rotate(self, params):
        self._rotation_count += 1
        new_key = f"sk-rotated-{self._rotation_count:04d}-prod-live"
        # Push the new secret into config — this triggers the Signal
        self.rt.config.set("apps.db-client.targets.prod.api_key", new_key)
        print(f"  [vault] Rotated key to {new_key[:12]}...")
        return {"rotated": True, "rotation": self._rotation_count}


# ── Main ────────────────────────────────────────────────────────────

async def main():
    kernel = Kernel()
    kernel.discover([
        ConfigProvider, LoggingProvider, CredentialProvider,
        DatabaseClient, VaultSimulator,
    ])

    # Seed initial credentials in config
    kernel.instantiate("config", properties={
        "defaults": {
            "apps": {
                "db-client": {
                    "targets": {
                        "prod": {"api_key": "sk-initial-0000-prod-live"}
                    }
                }
            }
        }
    })

    await kernel.boot()

    print()
    db = kernel.lifecycle.get_instance("db-client").instance

    # Find runnable schemas directly
    # Initial state — effect already ran at boot
    status = await kernel.invoke("db-client.status", {})
    print(f"  Status: {status}")
    print(f"  Log: {db.connection_log}")

    # Simulate vault rotating the key 3 times
    for i in range(3):
        print()
        print(f"  === Rotation {i + 1} ===")
        await kernel.invoke("vault.rotate", {})
        # Give async effects a tick to run
        await asyncio.sleep(0.01)

    print()
    status = await kernel.invoke("db-client.status", {})
    print(f"  Final status: {status}")
    print(f"  Full log: {db.connection_log}")
    assert len(db.connection_log) == 4  # 1 initial + 3 rotations

    await kernel.shutdown()
    print()
    print("  Secret rotation demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
