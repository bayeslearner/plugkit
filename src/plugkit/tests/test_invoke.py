"""Port of packages/core/tests/invoke.spec.ts."""

from plugkit.cordis import Context, Service


async def test_functional_service():
    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")
            self.config = config

        def __cordis_invoke__(self, init=None):
            # `self` is a view: `self.config` reads the service, `self.ctx` the caller
            result = dict(self.config or {})
            obj = self.ctx
            chain = []
            while obj is not None:
                chain.append(obj)
                obj = obj.__dict__.get("_parent")
            for obj in reversed(chain):
                intercept = obj.__dict__.get("_intercept")
                if intercept is not None and "foo" in intercept:
                    result.update(intercept["foo"] or {})
            if init:
                result.update(init)
            return result

        def invoke(self):
            return self()

        def extend_(self, config=None):
            return self.__cordis_extend__({"config": {**self.config, **(config or {})}})

    root = Context()
    await root.plugin(Foo, {"a": 1})

    # access from context
    assert root.foo() == {"a": 1}
    ctx1 = root.intercept("foo", {"b": 2})
    assert ctx1.foo() == {"a": 1, "b": 2}
    foo1 = ctx1.foo
    assert isinstance(foo1, Foo)

    # create extension
    foo2 = root.foo.extend_({"c": 3})
    assert isinstance(foo2, Foo)
    assert foo2() == {"a": 1, "c": 3}
    foo3 = foo1.extend_({"d": 4})
    assert isinstance(foo3, Foo)
    assert foo3.invoke() == {"a": 1, "b": 2, "d": 4}

    # context traceability
    assert foo1.invoke() == {"a": 1, "b": 2}


async def test_uses_the_service_shadow_for_callable_extensions():
    class Dependency(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "dependency")

    class Callable(Service):
        inject = ["dependency"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "callable")

        def __cordis_invoke__(self):
            return self.ctx.dependency

        def extend_(self):
            return self.__cordis_extend__()

    class Outer(Service):
        inject = ["callable"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "outer")

        def call(self):
            callable_service = self.ctx.callable
            return [callable_service(), callable_service.extend_()()]

    root = Context()
    await root.plugin(Dependency)
    await root.plugin(Callable)
    await root.plugin(Outer)

    result = []

    async def apply(ctx, config):
        result.extend(ctx.outer.call())

    await root.inject(["outer"], apply)

    assert isinstance(result[0], Dependency)
    assert isinstance(result[1], Dependency)
