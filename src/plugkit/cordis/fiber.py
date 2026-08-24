"""Fiber: plugin lifecycle, effects, and state management."""

from __future__ import annotations

import asyncio
import inspect
from enum import IntEnum
from typing import Any, Callable, Optional

from .utils import (
    ChainedDict,
    DisposableList,
    build_outer_stack,
    compose_error,
    is_async_iterable,
    is_constructor,
    is_iterable,
    is_nullish,
    symbols,
)

INACTIVE = "__INACTIVE__"


class FiberState(IntEnum):
    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5


class CordisError(Exception):
    """Base error carrying a symbolic cordis code."""

    CODE = {"INACTIVE_EFFECT": "cannot create effect on inactive context"}

    def __init__(self, code: str, message: Optional[str] = None):
        self.code = code
        super().__init__(message if message is not None else CordisError.CODE.get(code, code))


class ValidationError(TypeError):
    def __init__(self, issues: list):
        self.issues = issues
        lines = []
        for issue in issues:
            path = getattr(issue, "path", None)
            message = getattr(issue, "message", str(issue))
            if path:
                lines.append(f"  - {message} (at {'.'.join(map(str, path))})")
            else:
                lines.append(f"  - {message}")
        super().__init__("invalid config:\n" + "\n".join(lines))


def resolve_config(runtime, config: Any) -> Any:
    if runtime.Config is None:
        return config
    result = runtime.Config.validate(config)
    if inspect.isawaitable(result):
        raise TypeError("Async config validation is not supported")
    issues = getattr(result, "issues", None)
    if issues:
        raise ValidationError(issues)
    return result.value


def _is_awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


async def _then(task, callback):
    await task
    result = callback()
    if _is_awaitable(result):
        await result
    return result


async def _await_or_none(result):
    if _is_awaitable(result):
        await result
    return None


class _NoopDispose:
    def __await__(self):
        async def run():
            return None

        return run().__await__()


def _schedule(coro) -> None:
    """Start a coroutine fire-and-forget, like an unawaited JS promise."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    asyncio.ensure_future(coro, loop=loop)


class _PendingTask:
    """A lazily-started coroutine — started eagerly when a loop is running."""

    def __init__(self, coro, on_done: Optional[Callable] = None):
        self._coro = coro
        self._task: Optional[asyncio.Task] = None
        self._on_done = on_done
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            self._start()

    def _start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._coro)
            if self._on_done is not None:
                self._task.add_done_callback(self._on_done)

    def __await__(self):
        self._start()
        return self._task.__await__()


class _LazyTask:
    """Like `_PendingTask`, but with an eagerly-run synchronous prefix."""

    def __init__(self, sync_start: Callable, coro_factory: Callable[[], Any]):
        sync_start()
        self._factory = coro_factory
        self._task: Optional[asyncio.Task] = None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            self._task = asyncio.ensure_future(coro_factory())

    def __await__(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self._factory())
        return self._task.__await__()


def _make_disposer(disposables: list) -> Callable:
    def dispose():
        task = None
        snapshot = disposables[:]
        disposables.clear()
        for d in reversed(snapshot):
            if task is not None:
                task = _then(task, d)
            else:
                result = _start_now(d())
                if _is_awaitable(result):
                    task = result
        return task

    return dispose


def _start_now(result, log=None):
    """Start a coroutine eagerly when a loop is running (JS async fns start
    immediately); returns an `asyncio.Task` when possible."""
    if _is_awaitable(result) and not isinstance(result, asyncio.Task):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return result
        task = asyncio.ensure_future(result)
        if log is not None:

            def done(t: asyncio.Task) -> None:
                if t.cancelled():
                    return
                err = t.exception()
                if err is not None:
                    log(err)

            task.add_done_callback(done)
        return task
    return result


def _safe_collect(collect: Callable, value: Any) -> None:
    if callable(value):
        collect(value)
    elif not is_nullish(value):
        raise TypeError("Invalid effect")


class _EffectRunner:
    __slots__ = ("epoch", "execute", "collect", "get_outer_stack")

    def __init__(self, epoch, execute, collect, get_outer_stack):
        self.epoch = epoch
        self.execute = execute
        self.collect = collect
        self.get_outer_stack = get_outer_stack


class FiberEffect:
    """The disposer returned by `ctx.effect()` — callable and awaitable."""

    __slots__ = ("_runner", "_task", "_dispose", "_meta", "_ctx")

    def __init__(self, ctx, runner: _EffectRunner, task, dispose: Callable, meta: dict):
        self._ctx = ctx
        self._runner = runner
        self._task = task
        self._dispose = dispose
        self._meta = meta

    @property
    def __cordis_effect__(self):
        return self._meta

    def __call__(self):
        if not self._runner.epoch:
            return _NoopDispose()
        self._runner.epoch = False
        if self._task is not None:
            result = _then(self._task, self._dispose)
        else:
            result = _await_or_none(self._dispose())
        return _start_now(result, log=self._ctx.logger.error)

    def __await__(self):
        async def run():
            if self._task is not None:
                await self._task
            return self

        return run().__await__()


class Runtime:
    """Plugin runtime record (JS `Plugin.Runtime`)."""

    __slots__ = ("name", "callback", "fibers", "Config")

    def __init__(self, name, callback, fibers, Config):
        self.name = name
        self.callback = callback
        self.fibers = fibers
        self.Config = Config


def _make_guard(ctx, dispose: Callable):
    def done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        try:
            result = dispose()
            if _is_awaitable(result):
                sub = asyncio.ensure_future(result)

                def sub_done(st: asyncio.Task) -> None:
                    if st.cancelled():
                        return
                    err = st.exception()
                    if err is not None:
                        ctx.logger.error(err)

                sub.add_done_callback(sub_done)
        except BaseException as error:  # pragma: no cover
            ctx.logger.error(error)

    return done


class Fiber:
    uid: Optional[int]
    config: Any
    state: FiberState = FiberState.PENDING
    store: Optional[dict]
    inertia: Optional[Any]

    def __init__(self, parent, config: Any, inject: dict, runtime: Optional[Runtime], get_outer_stack: Callable[[], list]):

        def collect(dispose) -> None:
            self._disposables.push(dispose)

        self.parent = parent
        self.inject = inject
        self.runtime = runtime
        self.entry = None  # set by the loader's internal/plugin listener
        self._hooks: dict[str, DisposableList] = {}
        self._disposables = DisposableList()
        self._error: Any = None
        self._store: dict = {}
        self.inertia = None

        if runtime is not None:
            self.uid = parent.registry.counter
            self.ctx = self.context = parent.extend({"fiber": self})

            inject_entries = [(name, cfg) for name, cfg in self.inject.items() if not is_nullish(cfg)]
            if inject_entries:
                intercept = ChainedDict(parent=parent._effective_intercept())
                for name, cfg in inject_entries:
                    intercept[name] = cfg
                self.ctx.__dict__["_intercept"] = intercept

            self._runner = _EffectRunner(INACTIVE, self._execute_plugin, collect, get_outer_stack)

            self.context.emit("internal/plugin", self)

            for name in list(self.inject.keys()):
                self._check_impl(name)

            fiber_self = self

            def plugin_effect():
                remove = runtime.fibers.push(fiber_self)
                try:
                    fiber_self.config = resolve_config(runtime, config)
                    fiber_self._refresh()
                except BaseException as error:
                    fiber_self.ctx.logger.error(error)
                    fiber_self._error = error

                def disposer():
                    # JS: an async disposer runs its synchronous prefix at
                    # call time — uid retirement and the registry bookkeeping
                    # must observe the state at the moment of disposal, not a
                    # loop turn later (HMR deletes and re-registers a runtime
                    # within one synchronous sequence).
                    fiber_self.uid = None
                    fiber_self.context.emit("internal/plugin", fiber_self)
                    if fiber_self.ctx.registry.has(runtime.callback):
                        remove()
                        if not runtime.fibers.length:
                            fiber_self.ctx.registry.delete(runtime.callback)
                    fiber_self._set_epoch(INACTIVE)

                    async def rest():
                        while fiber_self.inertia is not None:
                            await fiber_self.inertia

                    return rest()

                return disposer

            self.dispose = parent.fiber.effect(plugin_effect, "ctx.plugin()")
        else:
            self.uid = 0
            self.ctx = self.context = parent
            self.state = FiberState.ACTIVE
            self.store = {}
            self._runner = _EffectRunner("", lambda: None, collect, get_outer_stack)
            self.dispose = self.restart

    def _execute_plugin(self):
        runtime = self.runtime
        if is_constructor(runtime.callback) or getattr(runtime.callback, "__cordis_is_constructor__", False):
            instance = runtime.callback(self.ctx, self.config)
            for hook in _collect_method_injects(instance):
                hook()
            init = getattr(instance, symbols.init, None)
            return init() if init is not None else None
        return runtime.callback(self.ctx, self.config)

    @property
    def name(self) -> str:
        fiber = self
        while True:
            if fiber.runtime is not None and fiber.runtime.name:
                return fiber.runtime.name
            parent_fiber = fiber.parent.fiber
            if fiber is parent_fiber:
                return "root"
            fiber = parent_fiber

    def assert_active(self) -> None:
        if self.uid is not None:
            return
        raise CordisError("INACTIVE_EFFECT")

    def _execute(self, runner: _EffectRunner):
        old_epoch = runner.epoch

        def run_sync():
            effect = runner.execute()
            if callable(effect):
                runner.collect(effect)
                return None
            if is_nullish(effect):
                return None
            if _is_awaitable(effect):
                return self._run_awaitable_effect(effect, runner, old_epoch)
            if is_iterable(effect):
                for value in effect:
                    _safe_collect(runner.collect, value)
                return None
            if is_async_iterable(effect):
                return self._run_async_iterable_effect(effect, runner, old_epoch)
            raise TypeError("Invalid effect")

        return compose_error(run_sync, runner.get_outer_stack)

    async def _run_awaitable_effect(self, effect, runner: _EffectRunner, old_epoch):
        value = await effect
        _safe_collect(runner.collect, value)

    async def _run_async_iterable_effect(self, effect, runner: _EffectRunner, old_epoch):
        # mirror JS: force async stack trace before the first next()
        await asyncio.sleep(0)
        iterator = effect.__aiter__()
        while True:
            if runner.epoch != old_epoch:
                return
            try:
                value = await iterator.__anext__()
            except StopAsyncIteration:
                return
            _safe_collect(runner.collect, value)

    def effect(self, execute: Callable, label: str = "anonymous") -> FiberEffect:
        self.assert_active()

        disposables: list = []
        dispose = _make_disposer(disposables)

        meta: dict = {"label": label, "children": []}

        def collect(d):
            disposables.append(d)
            self._disposables.delete(d)
            effect_meta = getattr(d, "__cordis_effect__", None)
            if effect_meta is not None:
                meta["children"].append(effect_meta)

        runner = _EffectRunner(True, execute, collect, build_outer_stack())

        task = None
        try:
            task = self._execute(runner)
        except BaseException:
            dispose()
            raise

        if task is not None:
            pending = _PendingTask(task, on_done=_make_guard(self.ctx, dispose))
            task = pending

        wrapper = FiberEffect(self.ctx, runner, task, dispose, meta)
        remover = self._disposables.push(wrapper)
        disposables.append(remover)
        return wrapper

    def get_effects(self) -> list:
        result = []
        for d in self._disposables:
            meta = getattr(d, "__cordis_effect__", None)
            if meta is not None:
                result.append(meta)
        return result

    def _get_state(self) -> FiberState:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error is not None:
            return FiberState.FAILED
        if self._runner.epoch != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback) -> None:
        old_state = self.state
        result = callback()
        self.state = result if result is not None else self._get_state()
        if old_state is self.state:
            return
        self.context.emit("internal/status", self, old_state)

        if old_state != FiberState.ACTIVE and self.state != FiberState.ACTIVE:
            return
        for impl in list(self.ctx.reflect.store.values()):
            if impl.fiber is not self:
                continue
            self.ctx.reflect.notify([impl.name])

    def _check_impl(self, name: str) -> None:
        impl = self.ctx.reflect._get_impl(name, True)
        if impl is None:
            self._store.pop(name, None)
            return
        check = impl.check
        if check is not None:
            try:
                from .traceable import _InstanceView

                view = _InstanceView(impl.value, {"ctx": self.ctx})
                func = getattr(check, "__func__", check)
                if not func(view):
                    self._store.pop(name, None)
                    return
            except BaseException as error:
                impl.fiber.ctx.logger.error(error)
                self._store.pop(name, None)
                return
        self._store[name] = impl

    def _refresh(self) -> None:
        epoch = ""
        for name in self.inject.keys():
            impl = self._store.get(name)
            if impl is None:
                epoch = INACTIVE
                break
            epoch += f":{impl.fiber.uid}"
        self._set_epoch(epoch)

    def _set_epoch(self, epoch) -> None:
        old_epoch = self._runner.epoch
        if epoch == old_epoch:
            return
        self._runner.epoch = epoch
        if self.inertia is not None:
            return

        def transition():
            if epoch != INACTIVE and old_epoch == INACTIVE:
                self.inertia = self._start_reload()
                return FiberState.LOADING
            self.inertia = self._start_unload()
            return FiberState.UNLOADING

        self._update_state(transition)

    def _start_reload(self) -> _LazyTask:
        self.store = dict(self._store)
        old_epoch = self._runner.epoch
        fiber = self

        async def run():
            try:
                await asyncio.sleep(0)
                result = fiber._execute(fiber._runner)
                if _is_awaitable(result):
                    await result
            except BaseException as reason:
                fiber.ctx.logger.error(reason)
                fiber._error = reason
                fiber._runner.epoch = INACTIVE
            fiber._update_state(lambda: fiber._after_reload(old_epoch))

        return _LazyTask(lambda: None, run)

    def _after_reload(self, old_epoch):
        if self._runner.epoch == old_epoch:
            self.inertia = None
        else:
            self.inertia = self._start_unload()
            return FiberState.UNLOADING

    def _start_unload(self) -> _LazyTask:
        disposers = self._disposables.clear()
        fiber = self

        async def run():
            await asyncio.gather(*(fiber._dispose_safe(d) for d in disposers))
            fiber.store = None
            fiber._update_state(fiber._after_unload)

        return _LazyTask(lambda: None, run)

    def _after_unload(self):
        if self._runner.epoch == INACTIVE:
            self.inertia = None
        else:
            self.inertia = self._start_reload()
            return FiberState.LOADING

    async def _dispose_safe(self, dispose) -> None:
        try:
            result = dispose()
            if _is_awaitable(result):
                await result
        except BaseException as reason:
            self.ctx.logger.error(reason)

    async def await_(self):
        while self.inertia is not None:
            await self.inertia
        if self._error is not None:
            raise self._error
        return self

    # `await fiber` support
    def __await__(self):
        return self.await_().__await__()

    def restart(self):
        fiber = self.ctx.fiber
        fiber.assert_active()
        fiber._set_epoch(INACTIVE)
        fiber._refresh()
        return _start_now(fiber.await_())

    def update(self, config: Any, no_save: bool = False):
        fiber = self.ctx.fiber
        fiber.assert_active()
        config = resolve_config(fiber.runtime, config)
        return fiber.context.waterfall(fiber, "internal/update", config, no_save, _update_default(fiber, config))


def _update_default(fiber: "Fiber", config: Any):
    def inner(*args):
        fiber.config = config
        fiber._error = None
        return fiber.restart()

    return inner


def _collect_method_injects(instance: Any) -> list:
    """Collect `@Inject`-decorated methods (JS decorator initializers)."""
    from .traceable import _InstanceView

    tracker = getattr(instance, symbols.tracker, None)
    property_name = tracker.get("property") if tracker else None
    instance_ctx = getattr(instance, "ctx", None)
    hooks = []

    for cls in reversed(type(instance).__mro__):
        for fn in cls.__dict__.values():
            inject = getattr(fn, "__cordis_inject__", None)
            if not inject:
                continue

            def make(fn=fn, inject=inject):
                def hook():
                    def apply(ctx, config):
                        target = _InstanceView(instance, {property_name: ctx}) if property_name else instance
                        return fn(target)

                    instance_ctx.inject(inject, apply)

                return hook

            hooks.append(make())
    return hooks
