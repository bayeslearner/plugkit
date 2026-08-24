"""`ctx["database"]` — the same lookup as `ctx.database`, spelled honestly.

Services are found by name, never by type. The attribute form hides that well
enough that people read it as type-based injection; the subscript form cannot be
misread, and it reaches names that are not valid Python identifiers.
"""

import asyncio

import pytest

from plugkit import Context, provide


async def settle(n=15):
    for _ in range(n):
        await asyncio.sleep(0)


class Database:
    def __init__(self, dsn="sqlite://"):
        self.dsn = dsn


async def test_subscript_and_attribute_agree():
    root = Context()
    await root.plugin(provide(Database, "database"))
    await settle()
    assert root["database"].dsn == root.database.dsn == "sqlite://"


async def test_subscript_reaches_a_non_identifier_name():
    root = Context()
    await root.plugin(provide(Database, "db.primary"))
    await settle()
    assert root["db.primary"].dsn == "sqlite://"


async def test_membership_answers_can_i_use_this():
    root = Context()
    assert "database" not in root
    await root.plugin(provide(Database, "database"))
    await settle()
    assert "database" in root
    assert 42 not in root


async def test_missing_service_raises_keyerror_not_attributeerror():
    """Subscript access should fail the way a mapping fails."""
    root = Context()

    def reader(ctx, config=None):
        with pytest.raises(KeyError):
            ctx["nope"]

    await root.plugin(reader)
    await settle()


async def test_setitem_updates_an_already_provided_service():
    """Subscript-set mirrors attribute-set: it updates, it does not register.

    Registering is `ctx.provide(name, value)`, which returns the disposer that
    makes the registration reversible. A bare assignment has nowhere to put a
    disposer, so Cordis refuses it — same rule as refusing a read you did not
    inject.
    """
    root = Context()

    def publisher(ctx, config=None):
        ctx.provide("greeting", "hello")     # declare + register
        ctx["greeting"] = "goodbye"          # update

    await root.plugin(publisher)
    await settle()
    assert root["greeting"] == "goodbye"


async def test_setitem_refuses_an_undeclared_service():
    root = Context()

    def publisher(ctx, config=None):
        with pytest.raises((AttributeError, KeyError), match="without provide"):
            ctx["never_declared"] = "x"

    await root.plugin(publisher)
    await settle()


async def test_a_non_string_name_is_rejected():
    root = Context()
    with pytest.raises(TypeError, match="must be a string"):
        root[42]
    with pytest.raises(TypeError, match="must be a string"):
        root[42] = "x"
