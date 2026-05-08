"""Example 02 — Give and Take (Service Injection)

A temperature converter that requires a config service.
Domain: IoT / sensor data.
Shows: @provides, @requires, typed contracts, self.rt

Run: PYTHONPATH=. python examples/02_give_and_take.py
"""
import asyncio
from typing import Protocol, runtime_checkable
from pydantic import BaseModel
from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle


@runtime_checkable
class IUnitConverter(Protocol):
    def convert(self, value: float, from_unit: str, to_unit: str) -> float: ...


class ConvertParams(BaseModel):
    value: float
    from_unit: str = "C"
    to_unit: str = "F"


@component("converter")
@provides(IUnitConverter)
class TemperatureConverter:
    @lifecycle.activate
    def activate(self):
        print("Temperature converter ready")

    def convert(self, value: float, from_unit: str, to_unit: str) -> float:
        if from_unit == "C" and to_unit == "F":
            return value * 9 / 5 + 32
        if from_unit == "F" and to_unit == "C":
            return (value - 32) * 5 / 9
        return value


@component("sensor")
@requires(converter=IUnitConverter)
class SensorReader:
    @lifecycle.activate
    def activate(self):
        print("Sensor reader ready")

    @runnable("read", params=ConvertParams, description="Read sensor and convert")
    async def read(self, params):
        converted = self.rt.converter.convert(params.value, params.from_unit, params.to_unit)
        return {
            "original": f"{params.value}{params.from_unit}",
            "converted": f"{converted:.1f}{params.to_unit}",
        }


async def main():
    kernel = Kernel()
    kernel.discover([TemperatureConverter, SensorReader])
    await kernel.boot()

    print()
    # Find and call the runnable schema directly
    r = await kernel.invoke("sensor.read", {"value": 100, "from_unit": "C", "to_unit": "F"})
    print(f"  {r['original']} → {r['converted']}")

    r = await kernel.invoke("sensor.read", {"value": 72, "from_unit": "F", "to_unit": "C"})
    print(f"  {r['original']} → {r['converted']}")

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
