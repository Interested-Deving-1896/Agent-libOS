# Electron GUI

Agent libOS includes a local desktop management console for supervising
processes, messages, human approvals, checkpoints, capabilities, Skills,
JSON-RPC endpoints, audit records, and persisted LLM calls.

The GUI is a local-only Electron app. Electron starts
`agent-libos-gui-server`, receives a random session bearer token, and connects
to `127.0.0.1` through HTTP and Server-Sent Events. The renderer never receives
Node.js access; it uses the preload-exposed `libosApi` object.

During development the Electron main process starts the backend without a
shell. It first honors `AGENT_LIBOS_GUI_SERVER_BIN`, then tries the project
`.venv` entrypoint, and only falls back to `uv run agent-libos-gui-server` if no
local entrypoint exists.

## Architecture

```text
Electron main process
  -> starts Python agent-libos-gui-server
  -> owns the random GUI bearer token
  -> exposes limited preload IPC

React renderer
  -> calls localhost HTTP APIs
  -> subscribes to /api/events/stream
  -> renders process, message, approval, audit, and LLM state

Python GUI server
  -> owns Runtime.open(db)
  -> routes all operations through existing runtime managers
  -> never grants capability by GUI visibility
```

The GUI server is not a new security boundary. It is a local control surface
over the same primitives, Capability v2 checks, human approval flow, events,
and audit records used by the CLI. Its Python entrypoint lives under
`agent_libos.api.gui` with the CLI because both are host-facing API surfaces.

Closing the GUI server calls `Runtime.shutdown()`, which shuts down the host
control surface and closes the owned runtime resources, including the SQLite
store. It does not mark AgentProcess records as exited; process lifecycle
changes still go through the runtime `process.exit` primitive/tool path.

## Development

Install Python and GUI dependencies:

```bash
uv sync
npm --prefix gui install
```

Run the Python server directly:

```bash
uv run agent-libos-gui-server --db .agent_libos.sqlite --port 0
```

The server prints one JSON line containing the selected local URL and bearer
token:

```json
{"url":"http://127.0.0.1:51234","token":"...","db":".agent_libos.sqlite"}
```

Run the Electron app:

```bash
npm --prefix gui run electron:dev
```

Build and type-check the GUI:

```bash
npm --prefix gui run typecheck
npm --prefix gui run build
```

The Electron smoke path can be run headlessly with
`AGENT_LIBOS_GUI_SMOKE=1`. By default it verifies the Electron main process,
Python GUI server startup, authenticated `/api/health`, and graceful shutdown
without creating a BrowserWindow. Set `AGENT_LIBOS_GUI_SMOKE_WINDOW=1` when a
machine has a working desktop/GPU stack and you specifically want to exercise
the preload bridge.

The Vite development server is bound to `127.0.0.1` and restricts file serving
to the `gui/` directory. `npm audit --omit=dev` should remain clean; at the
time of this implementation npm reports a dev-only Vite/esbuild advisory with
no available upstream fix, so the development server must stay local-only.

## Current Workspace

The first screen is process-centered:

- left pane: process tree, status, image, cwd, unread message badges,
- center pane: selected process timeline and human request cards,
- right pane: details for overview, capabilities, tools/Skills, checkpoints,
  audit, LLM calls, JSON-RPC, and Object Memory summary,
- top bar: database, spawn, auto-run, quanta budget, run, step, pause, refresh.

The scheduler defaults to automatic mode. Users can pause auto-run, step a
selected process, or run the selected process with a bounded quantum count.
Automatic runs after spawn/message/exec may advance all runnable processes, but
`POST /api/processes/{pid}/run` is intentionally scoped to that pid. Real LLM
calls are still persisted in `llm_calls`, so the GUI can show token usage and
errors.

## High-Risk Operations

The GUI requires explicit confirmation for high-risk operations before sending
the final request to the server:

- process `exec` and `exit`,
- checkpoint-to-image commit,
- checkpoint restore and fork,
- capability grant, delegate, and revoke,
- JSON-RPC method calls,
- Skill registration and trust.

The confirmation dialog shows the pid/resource/action summary. The server also
rejects high-risk requests without `confirmed: true`, so accidental direct HTTP
calls fail closed before invoking the runtime operation.

JSON-RPC endpoint registration through the GUI accepts manifest text only. The
renderer cannot ask the Python GUI server to read an arbitrary host file path;
file/path based registration remains a CLI/admin workflow.

## API Summary

Important endpoints:

- `GET /api/health`
- `POST /api/shutdown`
- `GET /api/snapshot`
- `GET /api/events/stream?cursor=<id>`
- `GET /api/processes`
- `POST /api/processes`
- `POST /api/processes/{pid}/run|step|pause|resume|signal|message|interrupt|cd|exec|exit`
- `GET /api/processes/{pid}/messages|human-requests|llm-calls|audit|events|capabilities|checkpoints`
- `POST /api/human-requests/{request_id}/respond`
- `GET/POST /api/checkpoints`, `/api/skills`, `/api/capabilities`,
  `/api/images`, `/api/jsonrpc`, and `/api/modules`

All endpoints require `Authorization: Bearer <session-token>`.
