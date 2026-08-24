name = "greeter"
inject = ["database"]


def apply(ctx, config=None):
    prefix = (config or {}).get("prefix", "hello")
    ctx.provide("greeter", lambda who: f"{prefix} {who} via {ctx.database.dsn}")
