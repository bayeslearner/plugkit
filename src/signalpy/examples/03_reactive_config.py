"""Example 03 — Reactive Configuration

A web scraper whose URL is reactive. When the config service changes,
the @effect re-runs and the @computed URL recomputes — automatically.
Domain: data pipeline / web scraping.
Shows: @computed, @effect, reactive self.rt, Signal-backed config.

The config provider stores its state in a Signal. Calling config.set()
creates a new dict → Signal notifies → all consumers re-evaluate.
No manual re-injection needed.

Run: PYTHONPATH=src python -m signalpy.examples.03_reactive_config
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, computed, effect
from signalpy.providers.config import ConfigProvider


class ScrapeParams(BaseModel):
    pass


@component("scraper")
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
    # ConfigProvider with defaults — no YAML file needed
    kernel.discover([ConfigProvider, Scraper])
    kernel.instantiate("config", properties={
        "defaults": {
            "scraper": {"url": "http://example.com", "interval": 60}
        }
    })
    await kernel.boot()

    print()
    scraper = kernel.lifecycle.get_instance("scraper").instance
    config = kernel.registry.require("IConfig")

    # Initial state
    r = await kernel.invoke("scraper.scrape", {})
    print(f"  Scraping: {r}")
    print(f"  Effect log: {scraper.effect_log}")

    # Now change the config — config.set() triggers the Signal
    print()
    print("  --- Config changed (via config.set) ---")
    config.set("scraper.url", "http://production.com")
    config.set("scraper.interval", 30)

    r = await kernel.invoke("scraper.scrape", {})
    print(f"  Scraping: {r}")
    print(f"  Effect log: {scraper.effect_log}")

    # Can also change via direct method call on config
    print()
    print("  --- Config changed (direct) ---")
    config.set("scraper.url", "http://staging.com")

    r = await kernel.invoke("scraper.scrape", {})
    print(f"  Scraping: {r}")

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
