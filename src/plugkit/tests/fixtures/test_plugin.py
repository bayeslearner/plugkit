"""Port of packages/include/tests/fixtures/test-plugin.ts."""

name = "test-plugin"

value = "default"


def apply(ctx, config):
    ctx.on("test/get-value", lambda: value)
