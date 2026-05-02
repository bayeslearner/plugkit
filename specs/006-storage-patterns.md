# Storage, Auth, and Concurrency Patterns

## 1. Storage Architecture: Index + Filesystem

**Principle:** Index in SQL for fast queries. Content on filesystem for
flexibility. Filesystem is source of truth. Index is rebuildable.

```
┌────────────────────────────────┐
│  SQL Index (SQLAlchemy 2.0)    │  fast list/filter/paginate/join
│  SQLite (dev) / Postgres (prod)│  backend-portable via SQLAlchemy
├────────────────────────────────┤
│  Filesystem Content (fsspec)   │  case.json, artifacts, chat JSONL
│  local (dev) / S3 (prod)       │  backend-portable via fsspec
└────────────────────────────────┘
```

**SQLAlchemy** is to the database what **fsspec** is to the filesystem — a
portable Python interface so the code doesn't care which backend is underneath.

**Why SQLAlchemy 2.0:** backend portability (SQLite→Postgres, same code),
Alembic migrations, typed `Mapped[]` annotations, connection pooling.
The boring correct choice.

**The index is NOT the source of truth.** Drop the database, run
`rebuild_index_from_filesystem()`, everything comes back. The filesystem
is authoritative.

### Index schema

```python
class CaseIndex(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(primary_key=True)
    workspace_id: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(index=True)
    owner_id: Mapped[str] = mapped_column(index=True)
    label: Mapped[str] = mapped_column(index=True)
    title: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(index=True)
    updated_at: Mapped[datetime] = mapped_column(index=True)
    path: Mapped[str]           # filesystem path to case.json
    content_hash: Mapped[str]   # SHA256 of case.json for change detection
    version: Mapped[int]        # optimistic locking counter

class ArtifactIndex(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True, nullable=True)
    conversation_id: Mapped[str] = mapped_column(index=True, nullable=True)
    artifact_type: Mapped[str] = mapped_column(index=True)  # query, plan, code, result, ...
    name: Mapped[str]
    pinned: Mapped[bool] = mapped_column(default=False)  # ephemeral vs attached to case
    created_at: Mapped[datetime] = mapped_column(index=True)
    blob_hash: Mapped[str]      # SHA256 → content-addressed blob storage
    size_bytes: Mapped[int]

class ConversationIndex(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(primary_key=True)
    workspace_id: Mapped[str] = mapped_column(index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=True, index=True)
    owner_id: Mapped[str] = mapped_column(index=True)
    started_at: Mapped[datetime]
    updated_at: Mapped[datetime] = mapped_column(index=True)
    turn_count: Mapped[int]
    status: Mapped[str] = mapped_column(default="active")  # active, closed

class UserIndex(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True, index=True)
    email: Mapped[str] = mapped_column(nullable=True)
    auth_provider: Mapped[str]  # local, oidc, ldap, proxy
    created_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = mapped_column(nullable=True)
```

### Filesystem layout

```
<workspace>/
  cases/
    MAIN-0001/
      case.json              ← full case content (envelope + kind-specific details)
      artifacts/             ← pinned artifacts for this case
  _blobs/
    sha256/
      <hash>                 ← content-addressed blob storage (dedup)
  conversations/
    <conversation_id>.jsonl  ← append-only chat messages (one JSON per line)
  artifacts/
    ephemeral/
      <conversation_id>/
        <artifact_id>.json   ← working artifacts (not yet pinned to a case)
```

## 2. Content Versioning: Optimistic Locking

Last-write-wins with version check. NOT copy-on-write.

```python
async def put(self, case: Case) -> None:
    # Read current version from index
    current = self._index.get(case.id)
    if current and current.version != case.version:
        raise StaleVersionError(f"Case {case.id} was modified (expected v{case.version}, found v{current.version})")
    # Write content to filesystem
    case.version += 1
    await self._fs.write(case.path, case.to_json())
    # Update index
    self._index.upsert(case)
```

Audit trail for who-changed-what lives in the case's `lifecycle_events` list
(inside `case.json`), not in separate version snapshots. Old versions are
not preserved — if you need history, read the lifecycle events.

## 3. Chat/Conversation Concurrency: Lock, Don't Interleave

Interleaving messages from two writers into the same conversation makes no
sense — the LLM sees a garbled history and produces nonsensical responses.

**Rule:** One writer per conversation at a time. Second writer gets a clear
error: "This conversation is being used by another session."

```python
class ConversationStore:
    async def acquire_write_lock(self, conversation_id: str, session_id: str) -> bool:
        """Try to acquire write lock. Returns False if already held by another session."""
        # In-memory for single-process; Redis/Postgres advisory lock for multi-process
        ...

    async def append_turn(self, conversation_id: str, messages: list[dict]) -> None:
        """Append messages atomically. Caller must hold write lock."""
        # JSONL append with fsync
        ...
```

**Same user, two browser tabs:** Second tab gets the lock error. Frontend
shows "conversation open in another tab." This is correct behavior — the
LLM can't serve two concurrent conversations on the same thread.

**Different users, same case:** They get separate conversations scoped to
the same case. Each conversation has its own JSONL. Each has its own lock.
They can both investigate the same case simultaneously — they just can't
write to each other's conversation.

## 4. Artifacts: Ephemeral vs Pinned

During an investigation, the agent creates many working artifacts — SPL
queries, query results, analysis plans, code snippets, intermediate
findings. These are **ephemeral**: scoped to the conversation, not to
the case.

When the agent or human decides an artifact is worth keeping, it gets
**pinned** to a case. Pinning means:
- The blob stays in `_blobs/sha256/` (same content-addressed storage)
- The artifact index row gets `pinned=True` and `case_id` set
- A copy/link is placed in `cases/MAIN-0001/artifacts/`
- The artifact becomes part of the case's investigation record

**What changes when pinned:**
- Ephemeral artifacts are garbage-collected when the conversation closes
  or ages out (configurable retention, e.g., 7 days)
- Pinned artifacts live as long as the case lives
- Pinned artifacts are included in case exports, reports, handoffs
- Pinned artifacts appear in the case's artifact list in the UI

**What doesn't change:**
- The blob content is identical (same hash)
- The artifact metadata (type, name, created_at) is preserved
- No re-upload needed — pinning is an index update, not a data copy

```python
# Agent creates ephemeral artifact during conversation:
artifact_id = await store.create_artifact(
    conversation_id=conv_id,
    artifact_type="query_result",
    name="splunk_errors_last_24h",
    content=result_bytes,
)

# Human pins it to the case:
await store.pin_artifact(artifact_id, case_id="MAIN-0001")
```

**Examples of artifact types:**
- `query` — SPL query text the agent wrote
- `query_result` — tabular result of running that query
- `plan` — investigation plan the agent proposed
- `code` — Python/SPL code the agent generated
- `analysis` — written analysis/summary
- `screenshot` — captured UI state
- `diff` — before/after comparison
- `reference` — external doc/link the agent cited

## 5. Authentication: Header-Based Reverse Proxy + Fallback

Three auth modes, configured per deployment:

### Mode 1: Reverse proxy (enterprise)

A context-aware reverse proxy (Authentik, Authelia, Caddy forward-auth,
Cloudflare Access, etc.) handles authentication. The application trusts
headers:

```
X-Forwarded-User: jdoe
X-Forwarded-Email: jdoe@company.com
X-Forwarded-Groups: splunk-admins,sre-team
X-Forwarded-Preferred-Username: John Doe
```

The `IAuth` provider reads these headers. No local password storage.
No JWT minting. The proxy owns the session.

```python
@component("auth-proxy", version="1.0")
@provides("IAuth")
class ProxyAuthProvider:
    def authenticate(self, request) -> Identity:
        user = request.headers.get("X-Forwarded-User")
        if not user:
            raise AuthenticationError("No X-Forwarded-User header")
        return Identity(
            user_id=user,
            email=request.headers.get("X-Forwarded-Email", ""),
            groups=request.headers.get("X-Forwarded-Groups", "").split(","),
            provider="proxy",
        )
```

### Mode 2: Local JWT (single-user / dev)

Current prismi3 pattern: bcrypt password hash in workspace auth config,
JWT tokens issued by the app. Fine for dev and single-user deployments.

```python
@component("auth-local", version="1.0")
@provides("IAuth")
class LocalAuthProvider: ...
```

### Mode 3: OIDC (managed enterprise)

For deployments that want the app to handle auth directly (no proxy):
OIDC flow with Okta, Azure AD, Google Workspace.

```python
@component("auth-oidc", version="1.0")
@provides("IAuth")
class OIDCAuthProvider: ...
```

**The kernel doesn't know which mode.** The deployment picks which auth
component to discover. Consumers call `self.rt.auth.authenticate(request)`
regardless of mode.

**Authorization** is role/group-based. The `Identity` carries groups.
`@runnable(requires_role="admin")` or `@runnable(requires_action="cases.write")`
is enforced by the bus — same check regardless of auth provider.

## 6. Mapping to SignalPy Components

```python
# Storage
@component("case-store") @provides("ICaseStore")     # index + fs
@component("artifact-store") @provides("IArtifactStore")  # blobs + index
@component("conversation-store") @provides("IConversationStore")  # JSONL + locks

# Auth (pick one per deployment)
@component("auth-proxy") @provides("IAuth")
@component("auth-local") @provides("IAuth")
@component("auth-oidc") @provides("IAuth")
```

All implement contracts. All swappable via `kernel.discover()`.
Properties configure backends (SQLite vs Postgres, local vs S3, etc.).

---

## 7. Parity Plan: How to Rewrite Without Missing Anything

### The surface to cover

Prismi3 has:
- **100 API endpoints** (FastAPI routes)
- **47 tools** (registered via rt.tool())
- **2,264 test functions** across 193 test files
- **61 specs** documenting features
- **25 case kind registrations**
- **~20 case source definitions** (YAML cron jobs)
- **~10 skills** (markdown + frontmatter)

### The approach: inventory → contract → implement → verify

**Step 1: Build the parity ledger.**

Before writing any system-layer code, produce a machine-readable inventory
of every capability in prismi3:

```
parity/
  endpoints.yaml     # every API route: method, path, params, response shape
  tools.yaml         # every tool: name, params_model, description, timeout, destructive
  contracts.yaml     # every service contract: methods, providers, consumers
  data-shapes.yaml   # every Pydantic model used in APIs and tools
  events.yaml        # every event type published/subscribed
  config-keys.yaml   # every config key read by any component
```

Generate these from the actual code (grep + AST parse), not by hand.
This is the authoritative list of "what the old system does."

**Step 2: Define contracts first.**

For each group of capabilities, define the `Protocol` contract before
implementing. Compare the contract against the parity ledger — every
capability in the ledger must be covered by a contract method.

**Step 3: Port tests before code.**

For each contract:
1. Port the relevant prismi3 tests to the new repo
2. Make them fail (no implementation yet)
3. Implement until they pass

The tests ARE the specification. If prismi3's 2,264 tests pass against
the new system, you have parity. The tests catch behaviors the specs
don't document.

**Step 4: Side-by-side API comparison.**

For the API surface (100 endpoints), run both systems against the same
requests and diff the responses. Automated:

```python
# For each endpoint in endpoints.yaml:
old_response = await old_client.request(method, path, params)
new_response = await new_client.request(method, path, params)
assert old_response.status == new_response.status
assert old_response.json() == new_response.json()  # or structural match
```

**Step 5: E2E scenario replay.**

Prismi3 has ~320 E2E journeys. These are the highest-value tests —
they exercise real user workflows. Port the journey definitions and
run them against the new system.

### What "120%" means

100% = every capability in the parity ledger works in the new system.
120% = plus the 9 diagnosed problems are fixed:

1. One config, one truth, reactive (not 3 paths)
2. One tool registry (bus with schemas, not 4 registries)
3. Observable tool changes (Signal, not 60s cache)
4. Real DI (self.rt, not facade over globals)
5. Frontend push (reactive propagation, not polling)
6. Atomic writes (already fixed in spec60 M8)
7. Proper concurrency (conversation locks, not hope)
8. Artifact lifecycle (ephemeral → pinned)
9. Pluggable auth (proxy/local/OIDC, not hardcoded)

### The risk: behaviors that aren't tested

Prismi3 has 2,264 tests but the system is complex enough that some
behaviors exist only as side effects. Mitigations:

- **Read every route handler** before porting. Don't port from tests
  alone — read the implementation to find undocumented behaviors.
- **Shadow mode:** Run both systems in parallel in dev. New system
  handles reads, old system handles writes. Compare responses. Gradually
  shift writes to the new system.
- **User acceptance:** Before cutting over, have the actual user (you)
  use the new system for real work for a week. File every discrepancy.
