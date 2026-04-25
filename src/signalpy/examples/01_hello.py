"""Example 01 — Hello World

The simplest possible component. Boot, greet, shutdown.
Domain: none. Just shows the minimum kernel usage.

Run: PYTHONPATH=. python examples/01_hello.py
"""
import asyncio
from signalpy.kernel import Kernel, component, lifecycle, runnable
from pydantic import BaseModel


class GreetParams(BaseModel):
    name: str = "world"


@component("greeter")
class Greeter:
    @lifecycle.activate
    def activate(self):
        print("Greeter ready")

    @runnable("hello", params=GreetParams, description="Say hello")
    async def hello(self, params):
        return {"message": f"Hello, {params.name}!"}

    @lifecycle.deactivate
    def deactivate(self):
        print("Greeter stopped")


async def main():
    kernel = Kernel()
    kernel.discover([Greeter])
    await kernel.boot()

    result = await kernel.bus.invoke("greeter.hello", {"name": "Alice"})
    print(result)  # {'message': 'Hello, Alice!'}

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
