# CrewBoard

**A local web board that runs many headless AI-agent sessions at once and routes
work between them through a central "secretary" — so you stop being the
copy-paste relay.**

CrewBoard drives a fleet of headless [Claude Code](https://claude.com/claude-code)
CLI processes (`claude -p`) from a single browser board. Each agent stays focused
on its own task; a lightweight secretary watches every session and suggests when
one agent's output should be handed to another. You launch, watch, reply, and
relay — all from one screen, instead of shuttling text between terminals by hand.

Runs entirely on your **Claude Pro/Max subscription** — raw `claude -p`
subprocesses, **no Agent SDK, no `ANTHROPIC_API_KEY`**. Windows-native.

> Why "the copy-paste relay"? When you run several Claude sessions in parallel,
> *you* become the wire between them: copying one session's result into another's
> prompt, over and over. CrewBoard makes that a one-click **relay** and lets a
> secretary suggest the relays for you.

---

## Quick start

You need the **Claude Code CLI** on your `PATH`, signed in (`claude` once to log
in), and a **Claude Pro/Max** subscription — no API key. Then, on Windows:

```bash
git clone https://github.com/alperumul/crewboard
cd crewboard
python -m venv .venv
.venv/Scripts/python.exe -m pip install flask mcp
.venv/Scripts/python.exe app.py
# open http://127.0.0.1:5055
```

Launch a session (name + prompt) → watch its status light → **Reply** to continue
it, or **Relay** its output into another session with one click. Full details
below.

---

## What it does

- **Board of sessions** — every session is a card: name · status light · latest
  snippet. Click a card to focus it (full streamed thread + reply box + relay).
  Two views of the same board: a **Rail** (reading room) and a **Map**
  (switchboard graph of who-feeds-whom).
- **Status lights** — ⚪ running · 🟢 done (the session emitted its task-done
  sentinel) · 🟡 needs you (finished without the sentinel, or a tool wants
  approval) · 🔴 errored. *Absence of the sentinel is always 🟡, never a false 🟢.*
- **Reply** — resumes any session in place (`--resume`); blocked while it's busy.
- **Relay (the point)** — deliver one session's output into another, verbatim,
  with one click. No copy-paste.
- **Secretary (Sonnet, automatic)** — observes each session as it finishes a turn
  and surfaces a per-session brief, cross-session triage, and relay suggestions.
  Cheap, frequent, stateless passes; per-call cost is logged.
- **Advisor (Opus, on-demand)** — a single persistent, streaming session with real
  cross-turn memory that answers deep questions about the whole board.

### Architecture — a STAR topology

The secretary is the single central brain; sessions never talk to each other
directly. The hub deterministically *executes* every relay as a
`claude -p --resume` call.

```
app.py            Flask routes (launch / reply / stream-SSE / relay / permission / secretary)
hub/sessions.py   subprocess launch + stream + resume, status state machine, done-sentinel
hub/state.py      durable node ↔ session_id ↔ cwd ↔ last-message map (state.json), restart-proof
hub/secretary.py  secretary (Sonnet, event-driven) + advisor (Opus, on-demand)
hub/permissions.py + hub/mcp_server.py   the 🟡 approve flow (per-session MCP bridge)
static/           index.html · app.js · style.css  (vanilla JS, no build step)
spikes/           standalone verify_*.py scripts that pinned the risky integrations
```

A "session" is a durable Claude session id on disk
(`~/.claude/projects/<proj>/<id>.jsonl`); a "turn" is an ephemeral subprocess —
nothing long-lived to crash. State survives a restart: reload the map and
`--resume` still works.

---

## How to use

### Prerequisites

- **Windows** (the launcher uses `CREATE_NO_WINDOW`; paths are Windows-native).
- **Python 3.12+** (developed on 3.14).
- **Claude Code CLI** on your `PATH` and signed in — install per
  [claude.com/claude-code](https://claude.com/claude-code), then run `claude`
  once to log in. Built against CLI **v2.1.183**.
- A **Claude Pro or Max** subscription (the CLI uses it; no API key needed).

### Setup

```bash
# from the repo root
python -m venv .venv
.venv/Scripts/python.exe -m pip install flask mcp
```

*(There is no `requirements.txt` — the only two dependencies are `flask` and
`mcp`.)*

### Point it at a project (optional)

Sessions launch in a default working directory (`--resume` is directory-scoped,
so this is where session work happens). By default that's the repo root. To drive
another project, either:

- set the `CLAUDE_HUB_CWD` environment variable, **or**
- drop a one-line file named `.hub_cwd` next to `app.py` containing the absolute
  path (it's git-ignored).

### Run

```bash
.venv/Scripts/python.exe app.py
# then open http://127.0.0.1:5055
```

1. **Launch** a session — give it a name and an opening prompt.
2. Watch the **status light**; click the card to read the streamed thread.
3. **Reply** to continue a session, or **Relay** its output straight into another.
4. Let the **secretary rail** suggest relays and triage; ask the **advisor** when
   you need a board-wide answer.

---

## Permission model

By default the hub launches sessions with `--permission-mode bypassPermissions`
(**auto mode**): every session runs fully unattended and nothing pauses for
approval. This is a deliberate single-user, localhost-only choice.

The **🟡 approve flow** is fully wired but dormant: each session can spawn a
per-session MCP bridge (`hub/mcp_server.py`) so that, under a guarded permission
mode, risky tool calls (side-effecting Bash, `Write`, web, `git push`) surface an
allow/deny card in the UI and the *same turn continues* once you decide. Switching
back to a guarded mode is a one-constant change in `hub/sessions.py`.

## Security

Single-user, no auth — **bind to localhost only** (it does: `127.0.0.1:5055`).
Mutating requests are pinned to the `Host` header so a cross-origin page can't
drive the hub. Don't expose this port to a network.

## Verified `claude -p` facts (don't re-derive — see `spikes/`)

- `--verbose` is **required** for stream-json to actually stream under `-p`; the
  launch line is `--output-format stream-json --verbose --include-partial-messages`.
- `--resume <id>` is **directory-scoped** — always run from the session's launch cwd.
- The approve MCP tool must return a **bare** JSON string
  `{"behavior":"allow","updatedInput":…}` (a return-type annotation breaks it).
- A paused turn tolerates **≥180s** waiting for a human approval decision.

## Known limitations

- A session's full thread history isn't persisted across a hub restart (the card
  snippet is; the focus thread repopulates on the next turn).
- The secretary is stateless per observation (no rolling long-context yet).
- Single-user, localhost only.

## License

[MIT](LICENSE) © 2026 Alper Ümül

---

*Built solo as an internal tool to orchestrate my own multi-agent Claude
workflows. "Claude" and "Claude Code" are products of Anthropic; this project is
an independent wrapper around the public CLI and is not affiliated with Anthropic.*
