"""Hot Code Update Demo — self-contained.

1. Boots the kernel with a PluginLoader watching a temp directory
2. Copies search_v1.py into the plugins dir → PluginLoader discovers and hot_adds it
3. Indexes documents, runs searches
4. Copies search_v2.py into the plugins dir (overwriting v1) → PluginLoader hot_updates
5. Same documents, same query count — but v2 search algorithm

This is the real flow: .py file appears on disk → kernel picks it up.

Run: PYTHONPATH=src python -m signalpy.examples.hot_update_demo
"""
import asyncio
import shutil
import tempfile
from pathlib import Path

from signalpy.kernel import Kernel
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider
from signalpy.providers.plugin_loader import PluginLoader

# Paths to the versioned plugin files (shipped with this example)
HERE = Path(__file__).parent
SEARCH_V1 = HERE / "search_v1.py"
SEARCH_V2 = HERE / "search_v2.py"


async def main():
    # Create a temp plugins directory (simulates a deployment target)
    with tempfile.TemporaryDirectory(prefix="signalpy_plugins_") as tmpdir:
        plugin_dir = Path(tmpdir)
        print(f"  Plugin directory: {plugin_dir}")

        # ── Boot kernel with PluginLoader ─────────────────────
        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, PluginLoader])
        kernel.instantiate("config", properties={"defaults": {}})
        kernel.instantiate("plugin-loader", properties={
            "plugin_dir": str(plugin_dir),
            "kernel": kernel,
        })
        await kernel.boot()

        # ── Deploy V1: copy search_v1.py into plugins dir ────
        print()
        print("  === Deploy V1: copy search_v1.py into plugins/ ===")
        shutil.copy2(SEARCH_V1, plugin_dir / "search.py")

        loader = kernel.lifecycle.get_instance("plugin-loader").instance
        result = await loader.scan()
        print(f"    Scan result: {result}")

        # Index some documents
        print()
        print("  === Index documents ===")
        for doc_id, text in [
            ("1", "Python programming language"),
            ("2", "Python snake species"),
            ("3", "JavaScript framework"),
        ]:
            r = await kernel.invoke("search.index_doc", {"id": doc_id, "text": text})
            print(f"    Indexed: {r}")

        # Search with V1
        print()
        print("  === Search with V1 ===")
        r = await kernel.invoke("search.search", {"query": "python"})
        print(f"    Engine: {r['engine']}")
        print(f"    Results: {len(r['results'])} hits")
        for hit in r["results"]:
            print(f"      {hit['id']}: {hit['text']}")
        print(f"    Total queries: {r['total_queries']}")

        status = await kernel.invoke("search.status", {})
        print(f"    Status: {status}")

        # ── Deploy V2: overwrite search.py with V2 ───────────
        print()
        print("  === Deploy V2: overwrite search.py with v2 ===")
        shutil.copy2(SEARCH_V2, plugin_dir / "search.py")

        loader = kernel.lifecycle.get_instance("plugin-loader").instance
        result = await loader.scan()
        print(f"    Scan result: {result}")

        # Search with V2 — state should be preserved
        print()
        print("  === Search with V2 (state preserved) ===")
        status = await kernel.invoke("search.status", {})
        print(f"    Status: {status}")
        assert status["version"] == "2.0", f"Expected v2.0, got {status['version']}"
        assert status["docs"] == 3, f"Expected 3 docs, got {status['docs']}"
        assert status["queries"] == 1, f"Expected 1 query, got {status['queries']}"

        r = await kernel.invoke("search.search", {"query": "python"})
        print(f"    Engine: {r['engine']}")
        print(f"    Results: {len(r['results'])} hits (now with scores)")
        for hit in r["results"]:
            print(f"      {hit['id']}: {hit['text']} (score={hit.get('score', 'n/a')})")
        print(f"    Total queries: {r['total_queries']}")
        assert r["total_queries"] == 2, "Query count should continue from v1"

        # Plugin loader status
        print()
        loader_status = await loader.status()
        print(f"  === Plugin loader status ===")
        print(f"    Loaded factories: {loader_status['loaded_factories']}")
        print(f"    Load log: {loader_status['load_log']}")

        await kernel.shutdown()

    print()
    print("  Hot update demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
