"""Platform providers — Axis 2 components that ship with the kernel."""
from signalpy.providers.auth import AuthProvider
from signalpy.providers.config import ConfigProvider
from signalpy.providers.credentials import CredentialProvider
from signalpy.providers.gateway import APIGateway
from signalpy.providers.logging_provider import LoggingProvider
from signalpy.providers.storage import StorageProvider
from signalpy.providers.tracing import TracingProvider
from signalpy.providers.workspace import WorkspaceProvider

__all__ = [
    "AuthProvider",
    "ConfigProvider",
    "CredentialProvider",
    "APIGateway",
    "LoggingProvider",
    "StorageProvider",
    "TracingProvider",
    "WorkspaceProvider",
]
