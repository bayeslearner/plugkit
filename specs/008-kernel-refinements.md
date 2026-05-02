# Kernel Refinements — Issues Found Building the Platform

Discovered while building signalpy-platform. Each item has the context of
where it was hit, why it matters, and whether it's a kernel-level fix or
a platform-level workaround.

## 1. Param validation silently swallows errors (KERNEL FIX)

**Where:** `__init__.py:304-308` in `_make_bus_handler()`

```python
try:
    model = runnable_def.params_model
    if hasattr(model, "model_validate"):
        obj = model.model_validate(params)
        validated = Params(obj.model_dump())
except Exception:
    pass  # ← swallows ALL validation errors silently
```

**Problem:** If the LLM sends malformed params, they pass through unvalidated.
The tool handler receives garbage and fails with a confusing error deep inside
its logic instead of a clear "invalid params" error at the boundary.

**Fix:** Don't catch-all. Let ValidationError propagate or wrap it in a
structured error. The bus handler should return a validation error envelope,
not silently proceed with bad data.

```python
except ValidationError as ve:
    raise ValueError(f"Invalid params for {runnable_def.name}: {ve}")
```

## 2. No bus.invoke_stream() (KERNEL ENHANCEMENT)

**Where:** Discussed in specs/007 but never built.

**Problem:** `@runnable` returns a value. Streaming returns chunks over time.
The agent's SSE streaming currently works because DeepAgents handles it
internally, but if we ever need to stream results from a tool (e.g., a
long-running Splunk query that yields rows incrementally), the bus has no
streaming dispatch.

**Proposed:**
```python
async for chunk in bus.invoke_stream("splunk.query_stream", params):
    yield chunk
```

**Priority:** Low — DeepAgents handles agent streaming. Only needed if tools
themselves need to stream results.

## 3. discover() must be called before instantiate() (KERNEL UX)

**Where:** Every test and boot.py — the ordering is:
```python
kernel.discover([ConfigProvider, CaseStore, ...])  # must be first
kernel.instantiate("config", properties={...})      # then this
```

**Problem:** If you call `instantiate()` before `discover()`, you get
`TypeError: Unknown factory 'config'`. The error is clear, but the
ordering requirement is a usability paper cut. Every test needs to
remember this order.

**Options:**
- (A) Auto-discover on first instantiate (implicit)
- (B) Allow instantiate to queue until boot() (lazy)
- (C) Document and live with it (current)

**Recommendation:** (C) for now. The explicit ordering is fine. Document
it in CLAUDE.md.

## 4. StructuredTool needs both sync + async (PLATFORM CONCERN)

**Where:** `test_llm_integration.py` — DeepAgents calls tools via
LangGraph which runs tool functions in a thread pool (sync). But
bus.invoke() is async.

**Problem:** The tool bridge needs both:
```python
StructuredTool(
    func=sync_wrapper,      # for thread pool execution
    coroutine=async_wrapper, # for direct async execution
    args_schema=ParamsModel,
)
```

The sync wrapper uses `asyncio.run()` to call the async bus from a
thread pool thread. This works but is inelegant.

**Not a kernel issue.** The kernel's bus is correctly async. The bridge
between LangChain's sync tool execution and the async bus is the
platform's responsibility. Documented here for the record.

## 5. Hot-add/hot-remove needs verification for app bundles (KERNEL TEST)

**Where:** The kernel has `hot_add()` and `hot_remove()` from the original
signalpy implementation. But we haven't tested them with platform-scale
components (SplunkApp with 6 tools, tags, skills).

**Questions to verify:**
- Does `hot_remove()` unregister ALL bus handlers for the component?
- Does `hot_remove()` dispose all @effects on the component?
- Does `hot_add()` properly trigger the bus.handler_signal so the agent
  sees new tools?
- Can you `hot_remove()` then `hot_add()` the same component class
  (e.g., to reload a plugin)?
- What happens if component A depends on component B, and B is hot-removed?

**Priority:** High — the user wants hot-add/remove of app bundles.

## 6. Bus handler_signal fires per handler, not per batch (KERNEL PERF)

**Where:** `bus.py:register_handler()` calls
`self.handler_signal.set(frozenset(self._handlers.keys()))` on every
single registration.

**Problem:** When SplunkApp activates with 6 tools, the handler_signal
fires 6 times. Each fire triggers the agent's @effect to rebuild its
tool list. That's 6 unnecessary rebuilds.

**Fix:** Use `batch()` during component activation:
```python
# In kernel._register_component_bus():
with batch():
    for rd in ci.meta.runnables:
        self.bus.register_handler(...)
# handler_signal fires ONCE here
```

**Priority:** Medium — noticeable if an app has many tools, but not
blocking.

## 7. No bus middleware/interceptor pattern (KERNEL ENHANCEMENT)

**Where:** SafeInvoker wraps bus.invoke() at the platform level. But
there's no way to register bus-level middleware that runs on every
invoke() call.

**Use cases:**
- Timeout enforcement (SafeInvoker)
- OTel tracing spans
- Auth enforcement
- Logging/audit

**Currently:** Each concern wraps bus.invoke() separately. The agent calls
SafeInvoker.safe_invoke() instead of bus.invoke(). This works but means
the middleware chain is implicit.

**Proposed:** Optional middleware on the bus:
```python
bus.add_middleware(timeout_middleware)
bus.add_middleware(tracing_middleware)
bus.add_middleware(auth_middleware)
# Every bus.invoke() passes through the chain
```

**Priority:** Medium — SafeInvoker works for now. Middleware would be
cleaner but adds complexity.
