"""signalpy.testing — test helpers for kernel consumers.

Not a pytest plugin. Works with any test runner. Import what you need.

Usage:

    # Integration test — full kernel with async context manager
    async with KernelFixture(MyApp, config={"db.url": "sqlite://"}) as kernel:
        schema = next(s for s in kernel.runnables() if s.name == "search")
        result = await schema.handler({"q": "hello"})
        assert result["count"] >= 0

    # Unit test — isolated Runtime, no kernel
    rt = mock_runtime(config=FakeConfig({"greeting": "Hey"}))
    app = GreeterApp()
    app.rt = rt
    assert app.greeting() == "Hey"
"""
from __future__ import annotations

from typing import Any

from signalpy.kernel import Kernel, Bus, Runtime, Signal
from signalpy.kernel.lifecycle_manager import State


class KernelFixture:
    """Async context manager that boots a kernel and shuts it down.

    Includes ConfigProvider and LoggingProvider by default. Pass additional
    component classes as positional args. Pass config dict for DictSource
    defaults.

        async with KernelFixture(UserService, SearchApp, config={"key": "val"}) as kernel:
            schema = next(s for s in kernel.runnables() if s.name == "create")
            result = await schema.handler({"name": "x"})

    Options:
        config:     dict merged into a writable DictSource layer
        with_auth:  include AuthProvider (default False — no auth enforcement)
        policies:   kernel-level publish policies
    """

    def __init__(
        self,
        *components: type,
        config: dict[str, Any] | None = None,
        with_auth: bool = False,
        policies: dict[str, dict] | None = None,
    ):
        self._components = list(components)
        self._config = config
        self._with_auth = with_auth
        self._policies = policies

    async def __aenter__(self) -> Kernel:
        from signalpy.providers.config import ConfigProvider, ConfigLayer, DictSource
        from signalpy.providers.logging_provider import LoggingProvider

        classes = [ConfigProvider, LoggingProvider]

        if self._with_auth:
            from signalpy.providers.auth import AuthProvider
            classes.append(AuthProvider)

        classes.extend(self._components)

        self.kernel = Kernel(policies=self._policies)
        self.kernel.discover(classes)

        if self._config is not None:
            self.kernel.instantiate("config", properties={
                "layers": [
                    ConfigLayer("defaults", source=DictSource(self._config), writable=True),
                ],
            })

        await self.kernel.boot()
        return self.kernel

    async def __aexit__(self, *exc) -> None:
        await self.kernel.shutdown()


def mock_runtime(
    *,
    component_name: str = "test",
    factory_name: str = "test",
    properties: dict[str, Any] | None = None,
    bus: Bus | None = None,
    **services: Any,
) -> Runtime:
    """Build a Runtime with injected services for unit-testing component methods.

    Bypasses the kernel entirely. The returned Runtime has no reactive tracking
    unless you access services inside an Effect/Computed.

        rt = mock_runtime(config=my_fake_config, logger=my_fake_logger)
        app = MyApp()
        app.rt = rt
        app.some_method()
    """
    rt = Runtime(
        component_name=component_name,
        factory_name=factory_name,
        properties=properties or {},
        _bus=bus or Bus(),
    )
    for name, svc in services.items():
        rt.inject(name, svc)
    return rt


class FakeConfig:
    """Minimal IConfig stub for unit tests. No reactivity, no layers.

        rt = mock_runtime(config=FakeConfig({"app.url": "http://localhost"}))
    """

    def __init__(self, data: dict[str, Any] | None = None):
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".")
        cur = self._data
        for part in parts:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        cur = self._data
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def all(self) -> dict[str, Any]:
        return dict(self._data)


class FakeLogger:
    """Minimal ILogger stub that captures log calls for assertion.

        logger = FakeLogger()
        rt = mock_runtime(logger=logger)
        # ... run code ...
        assert logger.has("error", "connection failed")
    """

    def __init__(self):
        self.entries: list[dict[str, Any]] = []

    def info(self, msg: str, **kwargs: Any) -> None:
        self.entries.append({"level": "info", "msg": msg, **kwargs})

    def warning(self, msg: str, **kwargs: Any) -> None:
        self.entries.append({"level": "warning", "msg": msg, **kwargs})

    def error(self, msg: str, **kwargs: Any) -> None:
        self.entries.append({"level": "error", "msg": msg, **kwargs})

    def debug(self, msg: str, **kwargs: Any) -> None:
        self.entries.append({"level": "debug", "msg": msg, **kwargs})

    def has(self, level: str, substring: str) -> bool:
        """Check if any entry at the given level contains the substring."""
        return any(
            e["level"] == level and substring in e["msg"]
            for e in self.entries
        )

    def clear(self) -> None:
        self.entries.clear()
