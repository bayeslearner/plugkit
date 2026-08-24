import asyncio
import pytest
from plugkit.cordis import Context
from .conftest import sleep
from .loader_utils import install, MockLoader


async def test_scratch():
    root = Context()
    loader = await install(root)
    foo = loader.mock('foo', lambda ctx, config: ctx.on('internal/update', lambda *a: None))
    bar = loader.mock('bar', lambda ctx, config: ctx.on('internal/update', lambda *a: None))
    qux = loader.mock('qux', lambda ctx, config: ctx.on('internal/update', lambda *a: None))
    await loader.read([
        {'id': '1', 'name': 'foo'},
        {'id': '2', 'name': '@cordisjs/plugin-group', 'config': [
            {'id': '3', 'name': 'bar', 'config': {'a': 1}},
            {'id': '4', 'name': 'qux', 'disabled': True},
        ]},
    ])
    print('after read1 entries:', [(e.options.get('id'), e.options.get('disabled')) for e in loader.entries()])
    await loader.read([{'id': '1', 'name': 'foo'}, {'id': '4', 'name': 'qux'}])
    print('after read2 entries:', [(e.options.get('id'), e.options.get('disabled'), e.fiber.state if e.fiber else None) for e in loader.entries()])
    print('qux registered:', root.registry.has(qux), 'calls:', len(qux.calls))
    print('data:', loader.data)
