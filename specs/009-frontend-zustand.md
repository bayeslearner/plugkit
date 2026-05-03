# Frontend State: Zustand + SSE Bridge

Spec for the prismi3 frontend revamp. Store for reference — the other
project implements this.

## Problem

prismi3's frontend uses raw `useState` per component. Each component
fetches its own data. No shared state. Polling for changes. Stale UI.

## Solution

Zustand store + SSE subscription. Backend pushes state changes via
`/api/events`. Frontend store subscribes and updates slices.

## Architecture

```
Backend kernel (Signal/Effect)
    │
    │ SSE events: config_changed, case_updated, tools_changed
    ▼
Frontend EventSource('/api/events')
    │
    │ event → store.setState()
    ▼
Zustand store (slices: cases, tools, settings, auth, conversations)
    │
    │ useStore(s => s.cases) — selector-based subscription
    ▼
React component re-renders ONLY when its slice changes
```

## Store structure

```typescript
interface PlatformStore {
  // Data slices (populated from API, updated via SSE)
  cases: Case[]
  overview: { total: number; open: number }
  tools: ToolSchema[]
  settings: Record<string, any>
  user: User | null
  conversations: ConversationSummary[]
  tags: Tag[]
  skills: SkillSummary[]
  apps: AppStatus[]

  // UI state (local only, not from backend)
  selectedCaseId: string | null
  activeTab: string
  sidebarOpen: boolean

  // Actions
  fetchCases: (filters?: CaseFilter) => Promise<void>
  fetchTools: () => Promise<void>
  login: (username: string, password: string) => Promise<boolean>
  logout: () => void
  updateSetting: (key: string, value: any) => Promise<void>
}
```

## SSE bridge

```typescript
// sse.ts
let eventSource: EventSource | null = null

export function connectSSE(store: typeof useStore) {
  eventSource = new EventSource('/api/events')

  eventSource.addEventListener('config_changed', (e) => {
    const { key, value } = JSON.parse(e.data)
    store.setState(s => ({
      settings: { ...s.settings, [key]: value }
    }))
  })

  eventSource.addEventListener('case_updated', () => {
    store.getState().fetchCases()
  })

  eventSource.addEventListener('tools_changed', () => {
    store.getState().fetchTools()
  })

  eventSource.addEventListener('error', () => {
    // Reconnect on error
    setTimeout(() => connectSSE(store), 5000)
  })
}
```

## Why Zustand (not Redux/Jotai/MobX)

- **No providers** — accessible outside React (SSE listener)
- **Selector-based** — `useStore(s => s.cases)` only re-renders when cases change
- **Minimal** — ~1KB, no boilerplate
- **Sliceable** — split store by domain for large apps
- **Middleware** — persist, devtools, immer for free

## Relationship to kernel

| Layer | Reactivity model | What it manages |
|-------|-----------------|-----------------|
| Backend kernel | Signal/Computed/Effect | Component coordination, service propagation |
| SSE bridge | Push events | Backend → frontend sync |
| Frontend Zustand | Store + selectors | UI rendering, optimistic updates |

They're complementary halves — not competing. Kernel handles backend
reactivity (component-to-component). Zustand handles frontend reactivity
(data-to-UI). SSE bridges them.
