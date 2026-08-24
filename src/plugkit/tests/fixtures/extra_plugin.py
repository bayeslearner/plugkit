"""Port of packages/include/tests/fixtures/extra-plugin.ts."""

name = "extra-plugin"


def apply(ctx, config):
    ctx.on("test/get-extra", lambda: "extra")
