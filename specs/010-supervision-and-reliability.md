# Supervision Trees + Bus Reliability

Motivated by: "I want the kernel to be versatile and robust to errors."
After evaluating the actor model (Pykka) against our reactive kernel, the
conclusion was: don't adopt actors, but graft the specific reliability
patterns that make actor systems robust onto our existing lifecycle + bus.

This spec covers four features, ordered by priority and dependency:

1. **Bus invoke timeout** — prevent hangs
2. **Fire-and-forget invoke** — tell semantics for runnables
3. **Supervision strategies** — automatic restart on failure
4. **Dead letter channel** — audit trail for failed dispatches

## Why not the actor model

We evaluated Pykka (Python actor framework, real OS `threading.Thread` per
actor since Pykka 3 removed gevent/eventlet). The actor model's core
benefits — message passing, state encapsulation, location transparency —
are already provided by our Bus + Registry + BusTransport. What actors add
on top (thread-per-entity, sequential mailbox, ask/tell) conflicts with
our reactive foundation:

- **Thread-per-component** is wasteful for async I/O services (macOS caps
  ~2,048-4,096 threads per process, each with 512KB stack)
- **Mailbox buffering** breaks reactive semantics — `Signal.set()` must
  propagate synchronously (or batched), not "when the mailbox drains"
- **Synchronous ask** across actors causes deadlocks (Pykka's own docs
  ship a deadlock debugging example)

What actor systems DO have that we lack: **supervision trees** (Erlang/OTP)
and **bus-level reliability** (timeout, dead letter). This spec adds those
without changing the concurrency model.

---

## 1. Bus invoke timeout

### Problem

`Bus.invoke()` awaits the handler with no timeout. If a handler hangs
(network call, deadlock, infinite loop), the caller hangs forever. There is
no way to express "give up after N seconds" at the bus level.

`SafeInvoker` at the platform level wraps this, but that's opt-in and
external to the kernel.

### Design

Add an optional `timeout` parameter to `Bus.invoke()` and `Runtime.invoke()`:

```python
# Bus level
async def invoke(self, target: str, params: dict | None = None,
                 *, timeout: float | None = None) -> Any:
    ...
    if timeout is not None:
        return await asyncio.wait_for(handler(p), timeout=timeout)
    return await handler(p)

# Runtime level (passes through)
async def invoke(self, target: str, params: dict | None = None,
                 *, timeout: float | None = None) -> Any:
    ...
    return await self._bus.invoke(target, params, timeout=timeout)
```

When the timeout fires, `asyncio.TimeoutError` propagates to the caller.
The handler's task is cancelled (standard asyncio cancellation).

### Default timeout

No default — `timeout=None` means wait forever (backward compatible).
Individual components can set their own defaults:

```python
@effect
async def poll_upstream(self):
    try:
        result = await self.rt.invoke("upstream.fetch", {}, timeout=10.0)
    except asyncio.TimeoutError:
        self.rt.logger.warning("upstream.fetch timed out")
```

A kernel-wide default timeout could be added via policy later:

```python
kernel.set_policy("*", {"invoke_timeout": 30.0})
```

But that's a future enhancement, not part of this spec.

### Changes

| File | Change |
|------|--------|
| `bus.py` | Add `timeout` kwarg to `invoke()` |
| `runtime.py` | Pass `timeout` through to `_bus.invoke()` |

~10 lines changed. No new files.

---

## 2. Fire-and-forget invoke (invoke_nowait)

### Problem

Today we have two communication patterns:
- `bus.invoke(target, params)` → request/response (ask). Caller awaits.
- `bus.publish(event_type, data)` → fan-out events (tell). No return value.

Missing: **invoke a specific runnable without waiting for its result**.
Use cases: triggering a reindex, kicking off a background job, notifying a
specific component to do something. `publish()` is wrong because it's a
fan-out event, not a targeted invocation.

### Design

```python
# Bus level
def invoke_nowait(self, target: str, params: dict | None = None) -> None:
    """Schedule a handler invocation on the event loop. Fire-and-forget."""
    handler = self._handlers.get(target)
    if handler is None:
        resolved = self.resolve_handler_name(target)
        if resolved:
            handler = self._handlers.get(resolved)
    if handler is None:
        # Publish to dead letter channel (see section 4)
        self._dead_letter(target, params, reason="no_handler")
        return
    p = params or {}
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(self._invoke_nowait_wrapper(target, handler, p))
    except RuntimeError:
        log.warning("invoke_nowait(%s): no event loop", target)

async def _invoke_nowait_wrapper(self, target: str, handler, params: dict) -> None:
    """Wrapper that catches and logs errors from fire-and-forget invocations."""
    try:
        await handler(params)
    except Exception:
        log.exception("invoke_nowait handler error: %s", target)
        self._dead_letter(target, params, reason="handler_error")

# Runtime level
def invoke_nowait(self, target: str, params: dict | None = None) -> None:
    """Fire-and-forget invocation. Policy-checked."""
    if not self._check_permission(target, self._invoke_allow, self._invoke_deny):
        raise PermissionError(...)
    self._bus.invoke_nowait(target, params)
```

### Semantics

- Returns immediately. No Future, no result.
- Errors in the handler are logged + sent to dead letter channel.
- Policy checks (invoke_allow/deny) still apply.
- Target routing (L3 targeted) still works.
- Name resolution (hallucination hardening) still works.

### Changes

| File | Change |
|------|--------|
| `bus.py` | Add `invoke_nowait()`, `_invoke_nowait_wrapper()` |
| `runtime.py` | Add `invoke_nowait()` pass-through |

~30 lines added.

---

## 3. Supervision strategies

This is the big one. Erlang/OTP's supervision trees are what make actor
systems genuinely fault-tolerant. The insight: **supervision is orthogonal
to the actor execution model**. We can graft it onto our reactive lifecycle
without changing how components run.

### Concepts

**Supervisor** — a component that manages the lifecycle of child
components. When a child fails (activation error, runtime crash), the
supervisor decides what to do based on its **strategy**.

**Strategies** (from Erlang/OTP):

| Strategy | Behavior |
|----------|----------|
| `one_for_one` | Restart only the failed child |
| `one_for_all` | Restart ALL children (for interdependent siblings) |
| `rest_for_one` | Restart the failed child + everything started after it |

**Restart intensity** — "max N restarts within M seconds." If the limit
is exceeded, the supervisor itself fails (escalates to its parent).

### State machine extension

Current:
```
DISCOVERED → RESOLVED → ACTIVATING → ACTIVE → DEACTIVATING → STOPPED
                                  ↘ ERRORED (terminal)
```

With supervision:
```
DISCOVERED → RESOLVED → ACTIVATING → ACTIVE → DEACTIVATING → STOPPED
                  ↑              ↘ ERRORED
                  └── RESTARTING ←┘
                      (supervisor-managed, with backoff)
```

New state: **RESTARTING** — the component is in a supervised retry cycle.
It transitions back to RESOLVED when the supervisor schedules the next
attempt. If max restarts exceeded, it stays ERRORED (terminal).

```python
class State(Enum):
    DISCOVERED = auto()
    RESOLVED = auto()
    ACTIVATING = auto()
    ACTIVE = auto()
    DEACTIVATING = auto()
    STOPPED = auto()
    ERRORED = auto()
    RESTARTING = auto()   # NEW: supervised retry in progress
```

### Decorator: @lifecycle.supervision

```python
@component("my-orchestrator", version="1.0")
class Orchestrator:

    @lifecycle.supervision(
        strategy="one_for_one",
        max_restarts=3,
        within_seconds=60,
        backoff="exponential",    # "constant" | "linear" | "exponential"
        base_delay=1.0,           # initial delay in seconds
    )
    async def on_child_failure(self, child_name: str, error: Exception,
                               attempt: int, context: SupervisionContext):
        """Called when a supervised child fails.

        Return True to proceed with restart (per strategy).
        Return False to give up (child stays ERRORED).
        Raise to escalate to this component's supervisor.
        """
        self.rt.logger.warning(
            "Child %s failed (attempt %d): %s", child_name, attempt, error
        )
        # Optional: adjust child properties before restart
        if isinstance(error, ConnectionError):
            context.update_properties({"retry_endpoint": "fallback.url"})
        return True
```

### Metadata

```python
@dataclass
class SupervisionDef:
    fn: Callable                          # the callback
    is_async: bool
    strategy: str = "one_for_one"         # one_for_one | one_for_all | rest_for_one
    max_restarts: int = 3
    within_seconds: float = 60.0
    backoff: str = "exponential"          # constant | linear | exponential
    base_delay: float = 1.0
```

Added to `ComponentMeta`:
```python
@dataclass
class ComponentMeta:
    ...
    supervision_def: SupervisionDef | None = None
```

### SupervisionContext

Passed to the callback so it can inspect and influence the restart:

```python
@dataclass
class SupervisionContext:
    child_name: str
    child_factory: str
    error: Exception
    attempt: int                     # 1-indexed
    restarts_in_window: int          # total restarts within within_seconds
    strategy: str
    _properties_override: dict | None = None

    def update_properties(self, props: dict) -> None:
        """Override child properties for the restart attempt."""
        self._properties_override = props

    def escalate(self) -> None:
        """Stop retrying this child, fail the supervisor instead."""
        raise SupervisionEscalation(self.child_name, self.error)
```

### Backoff calculation

```python
def _compute_delay(base: float, attempt: int, strategy: str) -> float:
    if strategy == "constant":
        return base
    elif strategy == "linear":
        return base * attempt
    elif strategy == "exponential":
        return base * (2 ** (attempt - 1))
    return base
```

Capped at 60 seconds regardless of strategy (hardcoded ceiling to prevent
unreasonable delays).

### Restart window tracking

```python
@dataclass
class RestartTracker:
    """Tracks restart attempts within a sliding window."""
    timestamps: list[float] = field(default_factory=list)

    def record(self, now: float) -> None:
        self.timestamps.append(now)

    def count_within(self, now: float, window_seconds: float) -> int:
        cutoff = now - window_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        return len(self.timestamps)
```

Stored on `ComponentInstance`:
```python
@dataclass
class ComponentInstance:
    ...
    _restart_tracker: RestartTracker = field(default_factory=RestartTracker)
```

### Lifecycle manager: supervised activation

The core logic lives in `LifecycleManager`. When activation fails and the
component has a supervisor parent:

```python
async def _handle_activation_failure(
    self, ci: ComponentInstance, error: Exception, runtime_builder
) -> None:
    """Handle activation failure with supervision, if applicable."""
    supervisor_ci = self._find_supervisor(ci)
    if supervisor_ci is None:
        # No supervisor — stay ERRORED (existing behavior)
        return

    sup_def = supervisor_ci.meta.supervision_def
    now = time.monotonic()
    ci._restart_tracker.record(now)
    restarts = ci._restart_tracker.count_within(now, sup_def.within_seconds)

    if restarts > sup_def.max_restarts:
        log.error(
            "Supervisor %s: max restarts (%d) exceeded for %s",
            supervisor_ci.name, sup_def.max_restarts, ci.name
        )
        # Escalate: fail the supervisor itself
        await self._escalate_to_parent(supervisor_ci, ci.name, error)
        return

    attempt = restarts  # 1-indexed because we just recorded
    context = SupervisionContext(
        child_name=ci.name,
        child_factory=ci.meta.factory_name,
        error=error,
        attempt=attempt,
        restarts_in_window=restarts,
        strategy=sup_def.strategy,
    )

    # Call the supervisor's callback
    try:
        should_restart = sup_def.fn(supervisor_ci.instance, ci.name,
                                     error, attempt, context)
        if sup_def.is_async:
            should_restart = await should_restart
    except SupervisionEscalation:
        await self._escalate_to_parent(supervisor_ci, ci.name, error)
        return

    if not should_restart:
        log.info("Supervisor %s declined restart for %s", supervisor_ci.name, ci.name)
        return

    # Apply strategy
    ci.state = State.RESTARTING
    delay = _compute_delay(sup_def.base_delay, attempt, sup_def.backoff)
    delay = min(delay, 60.0)

    log.info(
        "Supervisor %s: restarting %s in %.1fs (attempt %d/%d, strategy=%s)",
        supervisor_ci.name, ci.name, delay, attempt, sup_def.max_restarts,
        sup_def.strategy,
    )

    await asyncio.sleep(delay)

    if sup_def.strategy == "one_for_one":
        await self._restart_one(ci, context, runtime_builder)
    elif sup_def.strategy == "one_for_all":
        await self._restart_all_children(supervisor_ci, runtime_builder)
    elif sup_def.strategy == "rest_for_one":
        await self._restart_from(supervisor_ci, ci, runtime_builder)
```

### Strategy implementations

```python
async def _restart_one(self, ci, context, runtime_builder):
    """Restart a single child component."""
    ci.state = State.RESOLVED
    ci.error = None
    ci.instance = None
    if context._properties_override:
        ci.properties.update(context._properties_override)
    await self.activate(ci.name, runtime_builder)

async def _restart_all_children(self, supervisor_ci, runtime_builder):
    """Restart ALL children of the supervisor (one_for_all)."""
    # Deactivate all active children in reverse order
    for child_name in reversed(list(supervisor_ci.children)):
        child_ci = self._instances.get(child_name)
        if child_ci and child_ci.state == State.ACTIVE:
            await self.deactivate(child_name, runtime_builder)
    # Re-activate all in original order
    for child_name in supervisor_ci.children:
        child_ci = self._instances.get(child_name)
        if child_ci:
            child_ci.state = State.RESOLVED
            child_ci.error = None
            child_ci.instance = None
            await self.activate(child_name, runtime_builder)

async def _restart_from(self, supervisor_ci, failed_ci, runtime_builder):
    """Restart the failed child + everything started after it (rest_for_one)."""
    children = list(supervisor_ci.children)
    idx = children.index(failed_ci.name) if failed_ci.name in children else 0
    to_restart = children[idx:]

    # Deactivate in reverse
    for child_name in reversed(to_restart):
        child_ci = self._instances.get(child_name)
        if child_ci and child_ci.state in (State.ACTIVE, State.ERRORED):
            if child_ci.state == State.ACTIVE:
                await self.deactivate(child_name, runtime_builder)
            child_ci.state = State.RESOLVED
            child_ci.error = None
            child_ci.instance = None

    # Re-activate in order
    for child_name in to_restart:
        child_ci = self._instances.get(child_name)
        if child_ci:
            await self.activate(child_name, runtime_builder)
```

### How supervision connects to parent/child

Today, `parent`/`children` on `ComponentInstance` is set manually by
`_spawn_child()`. For supervision, we formalize the relationship:

- When a component spawns children via `self.rt.spawn()`, those children
  are automatically supervised by the parent **if the parent has a
  `@lifecycle.supervision` callback**.
- The `_find_supervisor(ci)` method walks up the parent chain to find the
  nearest ancestor with `supervision_def != None`.

```python
def _find_supervisor(self, ci: ComponentInstance) -> ComponentInstance | None:
    """Walk up the parent chain to find the nearest supervisor."""
    current = ci
    while current.parent:
        parent = self._instances.get(current.parent)
        if parent and parent.meta.supervision_def:
            return parent
        current = parent
    return None
```

### Escalation

When a supervisor exceeds its max_restarts, it "fails upward." The
supervisor itself transitions to ERRORED, and ITS supervisor (if any) gets
notified:

```python
async def _escalate_to_parent(self, supervisor_ci, child_name, error):
    """Supervisor gives up — escalate to its own supervisor."""
    supervisor_ci.state = State.ERRORED
    supervisor_ci.error = SupervisionEscalation(child_name, error)
    log.error(
        "Supervisor %s escalated failure from %s",
        supervisor_ci.name, child_name,
    )
    # Recursive: this supervisor's parent may supervise it
    await self._handle_activation_failure(
        supervisor_ci, supervisor_ci.error, runtime_builder=...
    )
```

### Trait

Supervision gives a component the **Supervisable** trait at L1:

```python
SUPERVISABLE = "supervisable"
# Inferred when: component has @lifecycle.supervision defined
```

Reported in `kernel.status()` alongside existing traits.

### Example: supervised pipeline

```python
@component("pipeline-supervisor", version="1.0")
class PipelineSupervisor:

    @lifecycle.activate
    async def activate(self):
        self._fetcher = await self.rt.spawn("data-fetcher", properties={
            "endpoint": "https://api.example.com"
        })
        self._processor = await self.rt.spawn("data-processor")
        self._writer = await self.rt.spawn("data-writer")

    @lifecycle.supervision(
        strategy="rest_for_one",     # if fetcher fails, restart processor+writer too
        max_restarts=5,
        within_seconds=120,
        backoff="exponential",
        base_delay=2.0,
    )
    async def on_child_failure(self, child_name, error, attempt, context):
        self.rt.logger.warning("Pipeline child %s failed: %s", child_name, error)
        if attempt >= 3 and isinstance(error, ConnectionError):
            # Switch to fallback endpoint after 3 failures
            context.update_properties({"endpoint": "https://fallback.example.com"})
        return True

    @lifecycle.deactivate
    async def deactivate(self):
        self.rt.logger.info("Pipeline supervisor shutting down")
```

### What supervision does NOT do

- **No runtime error supervision.** Supervision handles activation/startup
  failures. An `@effect` that throws at runtime is caught by the reactive
  engine (`reactive.py:463`) and logged — it doesn't trigger supervision.
  Rationale: runtime errors in effects are transient (bad data, network
  blip). Activation errors are structural (missing config, broken dep).
  Different failure modes deserve different recovery mechanisms.

- **No automatic health-check restarts.** `@lifecycle.health` returns
  status but doesn't trigger restarts. A future spec could add a periodic
  health probe that transitions ACTIVE → ERRORED → supervision, but that's
  a separate concern.

- **No process-level isolation.** Supervision restarts components in the
  same process. If you need OOM/segfault isolation, you need
  process-level supervisors (systemd, Kubernetes). The kernel is
  deliberately single-process.

### Changes

| File | Change |
|------|--------|
| `lifecycle_manager.py` | New state `RESTARTING`, `SupervisionContext`, `RestartTracker`, `_handle_activation_failure()`, strategy methods, `_find_supervisor()` |
| `component.py` | `@lifecycle.supervision` decorator, `SupervisionDef` dataclass, wired into `_finalize_meta()` |
| `traits.py` | New `SUPERVISABLE` trait at L1 |
| `__init__.py` | Wire supervision into `boot()` activation error path, export new types |

~200 lines added across 4 files.

---

## 4. Dead letter channel

### Problem

When a bus invocation fails (no handler, handler error, timeout), the
failure is either raised to the caller or silently logged. There is no
audit trail, no way for an operator or monitoring component to see all
failed dispatches in one place.

### Design

The bus publishes failed invocations to a reserved event channel:

```python
# In Bus.__init__:
self.DEAD_LETTER_CHANNEL = "__dead_letter__"

def _dead_letter(self, target: str, params: dict | None,
                 reason: str, error: Exception | None = None) -> None:
    """Record a failed dispatch to the dead letter channel."""
    envelope = {
        "target": target,
        "params": params,
        "reason": reason,           # "no_handler" | "handler_error" | "timeout"
        "error": str(error) if error else None,
        "timestamp": time.time(),
    }
    log.warning("Dead letter: %s → %s", target, reason)
    # Fire-and-forget publish to dead letter subscribers
    for handler in self._subscribers.get(self.DEAD_LETTER_CHANNEL, []):
        try:
            handler(self.DEAD_LETTER_CHANNEL, envelope)
        except Exception:
            pass  # dead letter handler must not throw
```

### Integration points

| Scenario | Where dead letter fires |
|----------|------------------------|
| `invoke()` target not found | After name resolution fails |
| `invoke_nowait()` target not found | In `invoke_nowait()` before return |
| `invoke_nowait()` handler error | In `_invoke_nowait_wrapper()` catch |
| `invoke()` timeout | Optional — caller gets TimeoutError, dead letter records it |

### Subscribing to dead letters

```python
@component("dead-letter-monitor", version="1.0")
class DeadLetterMonitor:
    @lifecycle.activate
    def activate(self):
        self.rt.on("__dead_letter__", self._on_dead_letter)

    def _on_dead_letter(self, event_type, data):
        self.rt.logger.error(
            "Dead letter: target=%s reason=%s error=%s",
            data["target"], data["reason"], data.get("error")
        )
```

### Changes

| File | Change |
|------|--------|
| `bus.py` | Add `_dead_letter()` method, call from `invoke()`, `invoke_nowait()` |

~20 lines added.

---

## Implementation order

```
Phase 1 (immediate):  Bus timeout + invoke_nowait + dead letter
                      These are small, self-contained, testable.
                      ~60 lines across bus.py + runtime.py.

Phase 2 (next):       Supervision strategies
                      Larger, touches lifecycle + component + traits.
                      ~200 lines. Requires tests with multi-level
                      component trees.
```

Phase 1 has no dependency on Phase 2. Phase 2 uses dead letter internally
(failed supervised restarts publish to dead letter).

## Constitution check

| Rule | Compliance |
|------|------------|
| Everything is a component | Supervision is metadata on components, not a privileged subsystem |
| No globals, no singletons | RestartTracker is per-instance, strategies are per-supervisor |
| Kernel has zero business logic | Supervision is mechanism (when/how to restart), not policy (what to restart) |
| Transport is an adapter | Bus reliability is transport-agnostic — works for local + remote |
| Lifecycle is explicit | RESTARTING is a new explicit state in the state machine |
| The kernel is small | ~260 lines total across both phases |
