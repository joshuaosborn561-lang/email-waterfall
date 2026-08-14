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
getleads → AI Ark → LeadMagic → Prospeo → FullEnrich (email only, opt-in)
```

AI Ark is **second on both lanes**. For emails it accepts a LinkedIn URL, an AI Ark person id, name + domain, and/or phone (`POST /v2/people/export/single` after People Search when needed). It is not people-discovery-only.

Prospeo is fourth on the email lane (`POST /enrich-person`, verified email only). `max_tier` default is `prospeo`. FullEnrich never runs unless you pass `max_tier=fullenrich`.

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
  "max_tier": "prospeo",
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

## Railway / Claude web (HTTPS)

This is the same connector shape as `google-maps-mcp`: **Streamable HTTP at `/mcp`, no auth.**

### Deploy (from this repo)

```bash
npm i -g @railway/cli
railway login
railway init --name email-waterfall
railway up
railway domain          # prints https://….up.railway.app
railway variables set \
  MCP_TRANSPORT=streamable-http \
  SUPABASE_URL=https://azpapwtnrbzywlnxxecz.supabase.co \
  SUPABASE_SERVICE_ROLE_KEY=… \
  GETLEADS_API_KEY=… \
  AI_ARK_API_KEY=… \
  LEADMAGIC_API_KEY=…
# optional: FULLENRICH_API_KEY=…
```

Dockerfile already sets `MCP_TRANSPORT=streamable-http` and binds `HOST=0.0.0.0` / `PORT` (Railway injects `PORT`). Health check: `GET /health`.

### Add in Claude

1. **Settings → Connectors → Add custom connector**
2. URL: `https://<railway-host>/mcp`
3. Auth: none
4. Enable the connector in the chat, then ask it to enrich domains for `basco` or `peterson`

Long runs return `job_id` — poll `get_job_status`.

## Tests

```bash
pytest -q
```
