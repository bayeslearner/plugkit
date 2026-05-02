# Agent Architecture + FastAPI Inside Kernel Boot

## 1. FastAPI Wrapped Inside Kernel Boot

**Current prismi3:** FastAPI owns the lifecycle. `lifespan()` boots config,
apps, MCP, scheduler. The kernel is a servant of FastAPI.

**New model:** The kernel owns the lifecycle. FastAPI is the RESTTransport
component — it activates when the kernel tells it to.

```python
# Platform boot script (the new system.py):
import asyncio
import uvicorn
from signalpy.kernel import Kernel
from signalpy.providers.config import ConfigProvider, ConfigLayer, YAMLSource
from signalpy.adapters.rest import RESTTransport

async def main():
    kernel = Kernel()

    # Configure layers (platform decides policy)
    workspace = resolve_workspace()  # pre-kernel bootstrap
    kernel.instantiate("config", properties={
        "layers": [
            ConfigLayer("defaults", PydanticDefaultsSource(PlatformConfig)),
            ConfigLayer("workspace", lambda ctx: YAMLSource(workspace / "local.yaml")),
            ConfigLayer("runtime", lambda ctx: YAMLSource(workspace / "settings.yaml"), writable=True),
        ],
    })

    # Discover all components
    kernel.discover([
        ConfigProvider, LoggingProvider, AuthProvider,
        CaseStore, ConversationStore, ArtifactStore,
        AgentComponent, MCPBridge, ToolGateway,
        RESTTransport,  # ← FastAPI is just another component
    ])

    await kernel.boot()

    # RESTTransport created a FastAPI app during activation
    # Extract it and hand to uvicorn
    rest = kernel.registry.require("IRestAPI")
    uvicorn.run(rest.app, host="0.0.0.0", port=5275)

asyncio.run(main())
```

**What changes:**
- `RESTTransport.activate()` creates the FastAPI app, reads `bus.schemas()`
  to auto-generate routes, mounts middleware (auth, CORS, etc.)
- Routes are generated from `@runnable` + `@api("rest")` declarations —
  same as today but driven by the bus, not by manual `router.include()`
- The kernel's lifecycle manager controls activation order — config before
  auth before storage before agent before REST
- Shutdown: `kernel.shutdown()` deactivates in reverse order — REST first
  (stop accepting requests), then agent, then storage, then config

**What doesn't change:**
- uvicorn still serves the ASGI app
- FastAPI still handles HTTP routing, middleware, OpenAPI
- The REST API surface is identical to prismi3's 100 endpoints

## 2. Agent: LangChain DeepAgents with OpenAI-Compatible Provider

**Requirement:** Use `deepagents.create_deep_agent()` for the core agent
loop instead of the hand-rolled AgentClient.

### Why this makes sense

DeepAgents gives us for free:
- **Multi-turn with checkpointing** — `InMemorySaver` or persistent checkpointer
  handles conversation state, including crash recovery
- **Tool calling** — native tool_use support, works with any LangChain-compatible model
- **Subagents** — specialized sub-agents (triage, curator) with their own tools
- **Human-in-the-loop** — `interrupt_on` for approval gates (destructive tools)
- **Streaming** — `agent.stream()` yields events (text, tool_call, tool_result)
- **Context engineering** — system prompt, skills, memory as first-class concepts
- **Provider portability** — OpenAI-compatible API means any provider works

### How it maps to SignalPy

```python
@component("agent", version="1.0")
@requires(config="IConfig")
@provides("IAgentLoop")
@api("rest", prefix="/chat", version="v1")
class AgentComponent:

    @lifecycle.activate
    def activate(self):
        agent_cfg = self.rt.config.get_typed("agent", AgentConfig)

        # LLM via OpenAI-compatible provider
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=agent_cfg.model,
            base_url=agent_cfg.base_url,
            api_key=agent_cfg.api_key,
        )

        # Tools from bus schemas → LangChain tool format
        tools = self._bus_tools_as_langchain()

        # Checkpointer for multi-turn
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()

        # Create the deep agent
        from deepagents import create_deep_agent
        self._agent = create_deep_agent(
            model=llm,
            tools=tools,
            system_prompt=self._build_system_prompt(),
            checkpointer=checkpointer,
            interrupt_on={
                # Destructive tools need approval
                **{name: True for name, schema in self._destructive_tools()},
            },
        )

    @effect
    async def on_config_change(self):
        """Reactive: when model/provider config changes, rebuild the agent."""
        agent_cfg = self.rt.config.get_typed("agent", AgentConfig)
        # Re-create LLM with new config
        # Re-create agent with new LLM
        # Existing conversations continue with new model on next turn

    @effect
    async def on_tools_change(self):
        """Reactive: when bus handlers change, rebuild tool list."""
        handler_names = self.rt.bus.handler_signal.get()  # reactive read
        self._agent = self._rebuild_agent_with_tools()

    @runnable("send", params=ChatParams, description="Send a message")
    async def send(self, params):
        config = {"configurable": {"thread_id": params.conversation_id}}
        result = self._agent.invoke(
            {"messages": [{"role": "user", "content": params.message}]},
            config=config,
        )
        return result

    @runnable("stream", params=ChatParams, description="Stream a response")
    async def stream(self, params):
        config = {"configurable": {"thread_id": params.conversation_id}}
        events = []
        for step in self._agent.stream(
            {"messages": [{"role": "user", "content": params.message}]},
            config=config,
            stream_mode="values",
        ):
            events.append(step)
        return events

    def _bus_tools_as_langchain(self):
        """Convert bus handler schemas to LangChain tool format."""
        from langchain_core.tools import StructuredTool

        lc_tools = []
        for schema in self.rt.bus.schemas():  # reads from bus, not a global
            async def _invoke(tool_name=f"{schema.provider}.{schema.name}", **kwargs):
                return await self.rt.invoke(tool_name, kwargs)

            lc_tools.append(StructuredTool.from_function(
                func=_invoke,
                name=schema.name,
                description=schema.description,
                args_schema=schema.params_model,
            ))
        return lc_tools
```

### OpenAI-compatible provider

The model string format: `"openai:model-name"` with custom `base_url`.
Works with:
- Anthropic via proxy (Claude Code proxy at localhost:20128)
- OpenAI directly
- Local models via vLLM/Ollama
- Any OpenAI-compatible endpoint

Config:
```yaml
agent:
  model: "gpt-4.1"  # or claude-opus via proxy
  base_url: "http://localhost:20128"  # Claude Code proxy, or OpenAI, or vLLM
  api_key: "${env:OPENAI_API_KEY}"
```

### Subagents for specialized tasks

```python
# Triage subagent — cheap model, fast, discriminator-style
triage_subagent = {
    "name": "triage",
    "description": "Classify incoming cases by severity and type",
    "system_prompt": "You classify cases. Output structured JSON.",
    "tools": [classify_tool],
}

# Curator subagent — autonomous case management
curator_subagent = {
    "name": "curator",
    "description": "Autonomous case lifecycle management",
    "system_prompt": "You manage case lifecycle...",
    "tools": [case_tools],
}

agent = create_deep_agent(
    model=llm,
    tools=user_facing_tools,
    subagents=[triage_subagent, curator_subagent],
    system_prompt="You are an investigation assistant...",
    checkpointer=checkpointer,
)
```

### What we lose vs hand-rolled AgentClient

- **Full control over the loop** — DeepAgents owns the agent loop. We
  configure it but don't write it. This is intentional — the loop is
  not our differentiator, the tools and context are.
- **Custom aging/context management** — DeepAgents has its own context
  engineering. We may need middleware or hooks for prismi3-specific
  aging (summarizing old tool results).
- **Exact SSE format** — DeepAgents' streaming format may differ from
  prismi3's current SSE events. The REST transport needs to adapt.

### What we gain

- **Battle-tested agent loop** — LangGraph handles the state machine,
  checkpointing, tool dispatch, error recovery.
- **Provider portability** — switch models without changing agent code.
- **Subagent orchestration** — triage, curator, specialized investigators
  as subagents, not separate systems.
- **Human-in-the-loop** — `interrupt_on` for destructive tool approval,
  built into the framework.
- **Community ecosystem** — LangSmith tracing, LangGraph Studio debugging,
  LangServe deployment.

## 3. How Bus Tools Become DeepAgent Tools

The bridge between SignalPy's bus and DeepAgents' tool system:

```
@runnable on component
    ↓ kernel registers
bus handler + HandlerSchema
    ↓ AgentComponent reads
bus.schemas() → list of HandlerSchema
    ↓ AgentComponent converts
LangChain StructuredTool (name, description, args_schema, func)
    ↓ passed to
create_deep_agent(tools=[...])
    ↓ agent calls tool
StructuredTool.func(**kwargs)
    ↓ which calls
bus.invoke("component.runnable", kwargs)
    ↓ dispatched to
@runnable handler on the component
```

One conversion layer. The bus is the source of truth for tool schemas.
DeepAgents sees LangChain tools. The bus dispatches the actual calls.
Dynamic tool registration (MCP bridge) triggers `@effect` on the agent
→ agent rebuilds its tool list → DeepAgents sees the new tools.
