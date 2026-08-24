"""`ctx.loader` — an application described by a YAML file.

The README lists composition from a file as a feature. It was not exercised
anywhere until this file: the kernel's `Loader` is abstract, and every existing
test used a `MockLoader` defined in the test helpers. A shipped feature nobody
runs is a claim, not a feature.
"""

import asyncio
import textwrap

from plugkit import Context
from plugkit.services.loader import FileLoader, load_app


async def settle(n=60):
    for _ in range(n):
        await asyncio.sleep(0)


def write_app(tmp_path, body: str):
    path = tmp_path / "app.yml"
    path.write_text(textwrap.dedent(body))
    return str(path)


async def test_an_application_loads_from_a_file(tmp_path):
    path = write_app(tmp_path, """
        - id: db
          name: plugkit.tests.appfixture.database
          config:
            dsn: postgres://prod
        - id: greet
          name: plugkit.tests.appfixture.greeter
          config:
            prefix: hi
    """)

    root = Context()
    await load_app(root, path)

    assert "database" in root
    assert root.greeter("world") == "hi world via postgres://prod"


async def test_entry_order_in_the_file_does_not_matter(tmp_path):
    """`greeter` needs `database` and is listed first."""
    path = write_app(tmp_path, """
        - id: greet
          name: plugkit.tests.appfixture.greeter
        - id: db
          name: plugkit.tests.appfixture.database
    """)

    root = Context()
    await load_app(root, path)
    assert root.greeter("x") == "hello x via sqlite://"


async def test_config_from_the_file_reaches_the_plugin(tmp_path):
    path = write_app(tmp_path, """
        - id: db
          name: plugkit.tests.appfixture.database
          config:
            dsn: mysql://somewhere
    """)

    root = Context()
    await load_app(root, path)
    assert root.database.dsn == "mysql://somewhere"


async def test_a_plugin_can_be_registered_by_hand():
    """`register()` resolves a name without importing anything.

    Works for an entry created directly through `loader.create()`. It does
    **not** reach entries listed inside an included YAML file — those resolve
    through the include's own tree. See the docstring on `FileLoader.register`.
    """
    import types

    def apply(ctx, config=None):
        ctx.provide("database", "the fake")

    root = Context()
    await root.plugin(FileLoader)
    await settle(10)
    root.loader.register(
        "fake.database", types.SimpleNamespace(name="fake.database", apply=apply)
    )
    await root.loader.create({"name": "fake.database"})
    await settle()

    assert root.database == "the fake"


async def test_a_missing_dependency_leaves_the_entry_waiting(tmp_path):
    """A file listing only the consumer produces no error and no service."""
    path = write_app(tmp_path, """
        - id: greet
          name: plugkit.tests.appfixture.greeter
    """)

    root = Context()
    await load_app(root, path)
    assert "greeter" not in root
    assert "database" not in root
