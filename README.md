# Email Waterfall MCP

Standalone **DM / work-email enrichment waterfall**. It takes company domains (plus optional known people), walks paid vendors, and writes isolated rows to Supabase.

It is **not** a Google Maps scraper and **not** a website crawler. Those stay in `googlemaps-scraper` / `google-maps-mcp`. This service only consumes domains and people that already exist. Apify contact-info scrapers are not called.

## Clients

`client_tag` is required on every write. Any snake_case tag works — call `ensure_client` or just `enrich_waterfall` (auto-ensures). New tags write `public.{tag}_wf_companies` / `public.{tag}_wf_contacts`.

| Tag | Owner | ICP | Tables |
|---|---|---|---|
| `basco` | Carlos | Franchise new-car dealership rooftops near Clifton, NJ | `public.basco_companies` / `public.basco_contacts` |
| `peterson` | Kyle | Commercial roofing / GCs / PMs in Dallas-Fort Worth | `public.peterson_companies` / `public.peterson_contacts` |
| `goliath` | — | IT / security DMs | `public.goliath_wf_companies` / `public.goliath_wf_contacts` |
| `salesglider` | — | Owner titles | `public.salesglider_wf_companies` / `public.salesglider_wf_contacts` |
| *any new tag* | — | `profile=owner` (default) or `profile=service` | `public.{tag}_wf_*` |

Basco titles (ranked): Service Director → Fixed Ops Director → Service Manager → Warranty Manager → Director/VP of Service → GM / Dealer Principal (fallback).

Peterson / default owner titles: Owner, Founder, Principal, President, Partner, CEO, VP, Director, General Manager.

## Waterfall

```
getleads → Smartlead → AI Ark → LeadMagic → Prospeo → FullEnrich
```

Smartlead is the **included plan email finder** (name + domain via `POST .../find-emails`). Remaining allotment is read from `GET .../search-analytics` (`availableCredits`). When credits are spent, the cascade falls through to paid tiers. It is not used for DM people search.

AI Ark is next on **people, email, and cellphone** (not people-only):

- People/DM: People Search by domain + ranked titles
- Email: LinkedIn URL, AI Ark person id, name + domain, and/or phone → `POST /v2/people/export/single`
- Cellphone: LinkedIn URL or name + domain → `POST /v2/people/mobile-phone-finder` (5 credits on hit)

LeadMagic mobile-finder runs after AI Ark when a LinkedIn URL or work email is available. Prospeo `enrich_mobile` only runs if `max_tier` is raised to `prospeo` or `fullenrich`.

`max_tier` default is `leadmagic` (alias `lm`). Raise it to `prospeo` / `fullenrich` (alias `fe`) if you want later paid email tiers.

## Name + company (no domain)

Rows with `first_name` + `last_name` + `company_name` and no domain are tagged `mode=name_company`. They skip getleads and Smartlead and enter at AI Ark → LeadMagic → Prospeo → FullEnrich. `company_name` is sent to FullEnrich verbatim (never replaced with a domain string). When a tier returns an email, its domain is written onto the row for later tiers.

## MCP tool: `enrich_waterfall`

Pass **either** `rows` **or** `source`, never both. Prefer `source` so lead payloads stay out of chat.

```json
{
  "source": {
    "project_id": "kemvxzhcxvynmoutwdrh",
    "schema": "public",
    "table": "ew_names_ready",
    "where": "wf_status is null",
    "key_column": "id",
    "map": {
      "first_name": "first_name",
      "last_name": "last_name",
      "company_name": "company_name"
    }
  },
  "need": "email",
  "client_tag": "peterson_earthworks",
  "max_tier": "fullenrich",
  "estimate_only": true
}
```

`source` is read server-side with the service role, paged 500 rows at a time. Results still write `public.{client_tag}_wf_contacts`. When writeback columns exist on the source table (or `writeback=true` adds them), the run also patches `wf_status`, `wf_email`, `wf_email_status`, `wf_vendor`, `wf_updated_at`.

`estimate_only=true` returns row counts per mode, the tiers each mode will touch, and a per-vendor credit estimate. Zero spend. Required before any paid source run.

Inline `rows` still works as before (domain and/or name+company). Response is **counts / job_id / cost only** — never row payloads. Long HTTP runs return `job_id` — poll `get_job_status`.

Other tools: `health`, `ensure_client`, `list_clients`, `describe_client`, `get_job_status`, `list_background_jobs`.

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
