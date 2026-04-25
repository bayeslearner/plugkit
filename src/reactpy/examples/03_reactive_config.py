"""Example 03 — Reactive Configuration

A web scraper whose URL is reactive. When the config service changes,
the @effect re-runs and the @computed URL recomputes — automatically.
Domain: data pipeline / web scraping.
Shows: @computed, @effect, reactive self.rt, Signal change propagation.

Run: PYTHONPATH=. python examples/03_reactive_config.py
"""
import asyncio
from pydantic import BaseModel
from reactpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, computed, effect, Signal


@component("config-svc")
@provides("IConfig")
class SimpleConfig:
    """A config service whose data can be changed at runtime."""
    @lifecycle.activate
    def activate(self):
        self._data = {"scraper.url": "http://example.com", "scraper.interval": 60}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set_val(self, key, value):
        self._data[key] = value


class ScrapeParams(BaseModel):
    pass


@component("scraper", depends=["config-svc"])
@requires(config="IConfig")
class Scraper:
    @lifecycle.activate
    def activate(self):
        self.effect_log = []

    @computed
    def target_url(self):
        """Always returns the current URL. Recomputes when config changes."""
        return self.rt.config.get("scraper.url")

    @computed
    def interval(self):
        return self.rt.config.get("scraper.interval")

    @effect
    def on_config_change(self):
        """Auto-tracks config reads. Re-runs when config service changes."""
        url = self.rt.config.get("scraper.url")
        interval = self.rt.config.get("scraper.interval")
        self.effect_log.append(f"Configured: {url} every {interval}s")
        print(f"  [effect] Scraper configured: {url} every {interval}s")

    @runnable("scrape", params=ScrapeParams, description="Run a scrape")
    async def scrape(self, params):
        return {"url": self.target_url(), "interval": self.interval()}


async def main():
    kernel = Kernel()
    kernel.discover([SimpleConfig, Scraper])
    await kernel.boot()

    print()
    scraper = kernel.lifecycle.get_instance("scraper").instance
    config = kernel.lifecycle.get_instance("config-svc").instance

    # Initial state
    r = await kernel.bus.invoke("scraper.scrape", {})
    print(f"  Scraping: {r}")
    print(f"  Effect log: {scraper.effect_log}")

    # Now change the config — simulate a config push
    print()
    print("  --- Config changed ---")
    config._data["scraper.url"] = "http://production.com"
    config._data["scraper.interval"] = 30
    # The config SERVICE object didn't change (same instance), so we need to
    # re-inject to trigger the Signal. In a real system, ConfigAdmin would do this.
    # Here we simulate it:
    scraper_ci = kernel.lifecycle.get_instance("scraper")
    scraper_ci.instance.rt.inject("config", config)  # Signal.set → notifies

    r = await kernel.bus.invoke("scraper.scrape", {})
    print(f"  Scraping: {r}")
    print(f"  Effect log: {scraper.effect_log}")

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
