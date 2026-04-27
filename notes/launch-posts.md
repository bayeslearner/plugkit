# Launch posts — drafts

All drafts assume the same three links:

- Repo: <https://github.com/bayeslearner/signalpy-kernel>
- Docs: <https://bayeslearner.github.io/signalpy-kernel/>
- PyPI: <https://pypi.org/project/signalpy-kernel/>

The disclosure framing the author asked for (verbatim spirit, tightened):
> Built with Claude's help. Hoping it lands somewhere between "trash" and
> "god code" — asking Python folks who know reactive systems / DI / microkernels
> to tell me which mistakes I made.

Sequencing reminder:
1. **Show HN** (Tue–Thu, 8–10am ET).
2. Same week **Saturday** — r/Python "I made this" + r/madeinpython.
3. Within 7 days — PyCoder's, Python Weekly, discuss.python.org, Python Discord.
4. Cross-post writeup to dev.to.
5. Defer Lobste.rs until invited.

---

## 1. Show HN

**Title** (≤ 80 chars):

```
Show HN: SignalPy Kernel – a Vue-style reactive microkernel for Python backends
```

**Body** (HN's "url" field gets the repo; the body is optional but helpful):

```
A small (~2,600 LOC, 9 files, zero deps) reactive component microkernel for
Python services. Three primitives — Signal, Computed, Effect — plus
component wiring. Everything else (config, logging, credentials, storage,
REST/MCP/CLI transports) is just components on top.

The idea I wanted to test: can backend DI be reactive the way frontend
state is? When a config provider is hot-swapped or a value changes, every
@effect that read it re-runs automatically. No manual @on_change, no
re-injection hacks. self.rt.config inside an @effect is a tracked read.

What's actually in there:
- 13 decorators total (the whole API surface)
- @runnable methods are simultaneously REST endpoints, MCP tools, and CLI
  commands depending on which transport adapter you discover. Components
  never know which transport serves them.
- Hot code update: kernel.hot_update(NewClass) with @lifecycle.snapshot /
  @lifecycle.restore — the only feature that required kernel changes; ~80 lines.
- 298 tests; CI runs 3.10/3.11/3.12.

What I want from this post: I built it with Claude's help. I'm hoping it
lands somewhere between "trash" and "god code" and would love eyes from
people who know reactive engines (Vue 3, Solid, Preact Signals, MobX) or
microkernel/DI containers (iPOPO, Dapr, Engin/Fx). Specifically interested
in mistakes around: contextvar tracking across awaits, the cancel_on_supersede
semantics, and whether the Signal-backed config approach has a hole I'm
not seeing.

Repo: https://github.com/bayeslearner/signalpy-kernel
Docs (the whole guided tour, including a line-by-line annotated reactive
engine and a "reactive intent vs default" patterns shelf):
https://bayeslearner.github.io/signalpy-kernel/
pip install signalpy-kernel
```

**Comment-tending plan.** Be online for the first 2–3 hours after posting.
Answer technical questions in the comments. Don't be defensive about the
AI-assist disclosure — most reasonable HN readers won't care if the work
itself holds up.

---

## 2. r/Python "Saturday Daily Thread: I Made This"

**Where:** the pinned weekly Saturday thread on r/Python. Don't post a
standalone thread — the sub removes those. Top-level comment in the
official thread.

**Comment body:**

```
SignalPy Kernel — a Vue-style reactive component microkernel for Python
backends. ~2,600 LOC, 9 files, zero required dependencies.

The premise: every injected service is a Signal. Reading self.rt.config
inside an @effect or @computed is a tracked read, so when config changes
or a provider gets hot-swapped, every effect that depended on it re-runs
automatically. No manual @on_change, no re-injection.

13 decorators total — @component / @provides / @requires / @computed /
@effect / @lifecycle.* / @runnable / @api / @subscribe / @kind / @skill /
@prop / @exportable. The same @runnable is automatically a REST endpoint,
MCP tool, and CLI command depending on which transport adapter the kernel
discovers.

Built with Claude's help. I'm hoping it's somewhere between trash and
god code, and I'd really like Python folks who know reactive systems
(Vue 3, Solid, Preact Signals, MobX) or DI containers (iPOPO, Dapr,
Engin/Uber Fx) to tell me which mistakes I made — particularly around
contextvar tracking across awaits and the supersede semantics for
in-flight async effects.

- Repo: https://github.com/bayeslearner/signalpy-kernel
- Docs: https://bayeslearner.github.io/signalpy-kernel/
- pip install signalpy-kernel

Issues / Discussions on the repo are open. Honest reviews welcome.
```

---

## 3. r/madeinpython (standalone post)

**Title:**

```
SignalPy Kernel — a small reactive microkernel for Python services (review wanted)
```

**Body:** identical to the r/Python comment above. Add the `[Project]` flair if
the sub uses flair.

---

## 4. dev.to writeup (cross-post the docs intro)

**Title:**

```
Bringing Vue-style reactivity to Python backend DI: a 2,600-line microkernel
```

**Tags:** `python`, `showdev`, `opensource`, `architecture`

**Cover image:** none needed; the docs site's index has the architecture
diagram — screenshot that.

**Body skeleton** (write the long version yourself; this is the structure):

```
## The premise

Frontend frameworks figured out reactive state a decade ago. Backend DI
containers haven't. Every "config changed" callback in a Python service
is hand-written. What if the DI container itself was reactive?

## What I built

[200 words: kernel = Signal + Computed + Effect + component wiring;
two-axis architecture; 13 decorators; same @runnable serves REST/MCP/CLI.]

## A 60-second example

[paste the README's runnable example]

## What's interesting (technically)

[3–4 paragraphs:
- Signal-backed config: config.set("foo.url", v) auto-triggers every @effect
  that read foo.url. No re-injection, no @on_change.
- contextvars + tracking across awaits: why Python is actually more robust
  here than the Vue 3 source comment implies.
- cancel_on_supersede: why is_stale()+return isn't enough when an await is
  blocked.
- hot_update: ~80 lines of kernel code; everything else is just components.]

## What I'm asking for

Built with Claude's help. I'm a Python developer who's used iPOPO, Dapr,
and read enough of the Vue 3 / Solid sources to be dangerous — but I'm
not a kernel implementer by trade. I want experts who actually build this
stuff to tell me where I'm wrong. Specifically interested in:

- Reactive engine soundness (Signal/Computed/Effect)
- Threading model (contextvars + the GIL + asyncio crossover)
- Hot-update edge cases I haven't thought of

Reviews welcome on the repo.
```

---

## 5. discuss.python.org

**Category:** "Python Help" → it's the de facto project-share venue until
the dedicated channel lands. Topic title:

```
Project for review: SignalPy Kernel (Signal-based reactive microkernel)
```

**Body:** same as the r/Python comment. Tone is more formal here — drop
"trash and god code" language; replace with "I want experienced eyes on
this before I tell anyone else to use it."

---

## 6. Python Discord — `#show-and-tell`

**Body** (one Discord message, ~1,000 chars):

```
Hi all — built a reactive component microkernel for Python services and would
love feedback before promoting it more widely.

It's three primitives (Signal/Computed/Effect) + component wiring, ~2,600 LOC
in 9 files, zero required deps. Same @runnable becomes a REST endpoint,
MCP tool, or CLI command depending on which transport adapter you load.

Built with Claude's help — hoping it's somewhere between trash and
god code. Looking for folks who've worked with Vue 3 / Solid / Preact
Signals reactivity, or with DI containers like iPOPO/Dapr/Engin, to
poke at the design.

Repo: https://github.com/bayeslearner/signalpy-kernel
Docs: https://bayeslearner.github.io/signalpy-kernel/
```

---

## 7. PyCoder's Weekly submission

**URL:** <https://pycoders.com/submissions>

The form takes a URL + a short reason. Use:

- **Link:** https://bayeslearner.github.io/signalpy-kernel/
- **Reason / pitch:**

```
SignalPy Kernel — a reactive component microkernel for Python backends.
Three primitives (Signal/Computed/Effect) plus component wiring. ~2,600 LOC,
zero deps. Same @runnable methods become REST endpoints, MCP tools, or CLI
commands depending on which transport adapter is loaded. The docs site
includes a line-by-line annotated reactive engine and a "reactive intent
vs default" patterns shelf with seven recipes (batch, is_stale,
cancel_on_supersede, cross-thread writes, mutate-in-place, first-run,
between-runs cleanup) that should be useful to anyone building reactive
Python — not just users of this library.
```

---

## 8. Python Weekly (Rahul Chaudhary)

**Contact:** form/email at <https://www.pythonweekly.com>. Send the same
pitch as PyCoder's; mention you'd appreciate inclusion if it fits the issue.

---

## What to expect

- **HN**: front page or nothing. If it lands, expect 3–5 substantive
  technical comments worth engaging with and a long tail of bikeshed.
- **r/Python**: probably 5–20 upvotes in the Saturday thread. Quality of
  feedback is hit-or-miss.
- **r/madeinpython**: smaller audience but more receptive to project posts.
- **discuss.python.org**: slower, but the people who do reply tend to be
  serious — this is where you'll get the best reviews.
- **Python Discord**: real-time drive-by feedback; useful for catching
  obvious gotchas.
- **Newsletters**: 1–2 week delay. Pitch quality matters more than novelty.
- **dev.to**: amplification, not review. Useful for the writeup itself.

---

## Pre-launch checklist

- [ ] Verify `pip install signalpy-kernel` works on a clean venv (one final time)
- [ ] Verify <https://bayeslearner.github.io/signalpy-kernel/> renders cleanly in incognito
- [ ] Verify the README's 60-second example actually runs as written
- [ ] Open at least 1–2 GitHub Issues yourself with "good first review" tags
      (e.g., "Review the `cancel_on_supersede` semantics", "Audit the
      contextvar tracking on async @effect re-entry") so reviewers have an
      obvious place to plug in
- [ ] Enable GitHub Discussions on the repo if not already on
- [ ] Have the Show HN tab open Tuesday morning, post 8:30am ET, stay near
      the keyboard for 2–3 hours
