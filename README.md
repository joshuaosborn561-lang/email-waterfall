# Email Waterfall MCP

Standalone **DM / work-email enrichment waterfall**. It takes company domains (plus optional known people), walks paid vendors, and writes isolated rows to Supabase.

It is **not** a Google Maps scraper and **not** a website crawler. Those stay in `googlemaps-scraper` / `google-maps-mcp`. This service only consumes domains and people that already exist. Apify contact-info scrapers are not called.

## Clients

`client_tag` is required on every write.

| Tag | Owner | ICP | Tables |
|---|---|---|---|
| `basco` | Carlos | Franchise new-car dealership rooftops near Clifton, NJ | `public.basco_companies` / `public.basco_contacts` |
| `peterson` | Kyle | Commercial roofing / GCs / PMs in Dallas-Fort Worth | `public.peterson_companies` / `public.peterson_contacts` |

Basco titles (ranked): Service Director → Fixed Ops Director → Service Manager → Warranty Manager → Director/VP of Service → GM / Dealer Principal (fallback).

Peterson titles: Owner, Founder, Principal, President, Partner, CEO, VP, Director, General Manager.

## Waterfall

```
AI Ark (people) → getleads → LeadMagic → FullEnrich (email only, opt-in)
```

`max_tier` default is `leadmagic`. FullEnrich never runs unless you pass `max_tier=fullenrich`.

## MCP tool: `enrich_waterfall`

```json
{
  "rows": [
    {
      "domain": "paragonhonda.com",
      "company_name": "Paragon Honda",
      "first_name": "",
      "last_name": "",
      "title": "",
      "email": "",
      "place_id": "",
      "city": "",
      "state": ""
    }
  ],
  "need": "dm",
  "client_tag": "basco",
  "max_tier": "leadmagic",
  "target_titles": "Service Director, Fixed Operations Director, Service Manager, Warranty Manager, General Manager, Dealer Principal, GM",
  "require_title_match": true,
  "background": true
}
```

Response is **counts only**. Long HTTP runs return `job_id` — poll `get_job_status`.

Other tools: `health`, `describe_client`, `get_job_status`, `list_background_jobs`.

## Supabase writes

Project: `campaignintelligence` (`azpapwtnrbzywlnxxecz`)

- Companies upsert on `domain`. Duplicate domains in one batch are merged first (avoids Postgres `21000`).
- Contacts with email: `ON CONFLICT (domain, email) DO NOTHING`.
- Null-email contacts insert separately (no conflict target).

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill vendor + Supabase keys
python -m mcp_server   # stdio
```

Cursor MCP snippet:

```json
{
  "mcpServers": {
    "email-waterfall": {
      "command": "python3",
      "args": ["-m", "mcp_server"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

## Railway / Claude web

Dockerfile ships `MCP_TRANSPORT=streamable-http` on port 8000. Connector URL is `https://<host>/mcp`. No connector auth.

Required env: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, plus whichever vendor keys you want live (`AI_ARK_API_KEY`, `GETLEADS_API_KEY`, `LEADMAGIC_API_KEY`, optional `FULLENRICH_API_KEY`).

## Tests

```bash
pytest -q
```
