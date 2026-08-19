# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single Python product: the **Email Waterfall MCP** server (DM / work-email
enrichment). Everything runs from one Python process; there is no separate frontend/backend or
local database container. Supabase and the paid vendor APIs are hosted external dependencies.

### Environment
- Python 3.12 with a virtualenv at `.venv/`. The update script creates/refreshes it, so run any
  commands with `source .venv/bin/activate` first (or call `.venv/bin/python`).
- There is **no lint tooling configured** in this repo (no ruff/flake8/black/mypy). For a basic
  static check use `python -m py_compile` over the source files. Do not add a linter unless asked.

### Run / test / build (standard commands, see `README.md`)
- Tests: `pytest -q`. The suite is fully offline — it mocks all vendor HTTP calls and Supabase, so
  **no secrets or network access are required** to get it green (43 tests).
- Run locally (stdio, the default, used by `.cursor/mcp.json`): `python -m mcp_server`.
- Run as an HTTP service (what Docker/Railway use): `MCP_TRANSPORT=streamable-http PORT=8000 python -m mcp_server`.
  Endpoints: `GET /` (info), `GET /health` (liveness JSON), MCP at `POST /mcp` (Streamable HTTP, no auth).
- "Build" = the Docker image (`Dockerfile`, deployed via `railway.json`); there is no compile step.

### Non-obvious gotchas
- `enrich_waterfall` (the core enrichment tool) needs real secrets to do anything useful:
  `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` (or `SUPABASE_ANON_KEY`) **plus** at least one vendor
  key (`GETLEADS_API_KEY`, `AI_ARK_API_KEY`, `LEADMAGIC_API_KEY`, `PROSPEO_API_KEY`,
  `FULLENRICH_API_KEY`). Without them, `health` reports `supabase_configured: false` and all vendors
  `false`, and enrichment writes nothing. Put these in a gitignored `.env` (see `.env.example`) or as
  Cloud Agent secrets — never commit them.
- Config is read from `.env` at import time but the MCP tools call `_reload_settings()` on each
  invocation, so editing `.env` is picked up without restarting the server.
- Background jobs persist to the local filesystem under `data/jobs/` (auto-created); there is no job
  queue service to run.
- The MCP tools return their result as a JSON string inside the tool's text content (not structured
  output), so a client should parse `result.content[0].text` as JSON.
