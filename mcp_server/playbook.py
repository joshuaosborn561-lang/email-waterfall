"""Playbook injected into the MCP server so clients know the product boundary."""

INSTRUCTIONS = """
# Email Waterfall MCP

## What this is
A paid-vendor waterfall that takes **company domains** (plus optional known people)
and resolves decision-makers + work emails, then writes to isolated Supabase tables.

This is NOT a Google Maps scraper and NOT a website crawler. Do not call Maps,
Apify contact scrapers, or crawl team pages from this server. Those live in
`googlemaps-scraper` / `google-maps-mcp`. This service only consumes domains/people
that already exist.

## Clients (required `client_tag`)
Any snake_case `client_tag` works. Call `ensure_client` (or just `enrich_waterfall`,
which auto-ensures) to create write tables — no deploy needed for a new client.

- **basco** (Carlos) — service/fixed-ops titles; `basco_companies` / `basco_contacts`.
- **peterson** (Kyle) — owner titles; `peterson_companies` / `peterson_contacts`.
- **Any new tag** (e.g. goliath) — default owner ranked titles; writes
  `public.{tag}_wf_companies` / `public.{tag}_wf_contacts`.
  Pass `profile=service` for basco-style titles, or `target_titles` to override.

Never omit `client_tag`. Never write to a shared contacts table.

## Default flow
1. `enrich_waterfall` always auto-runs `ensure_client` first, including builtin
   tags (`peterson`, `basco`). Do not skip this — a missing `peterson_companies`
   table is a 404, not a "pass rows" problem.
2. Prefer `source_table` + `where` over inline `rows` so lead payloads stay
   out of chat. Peterson queue: `client_peterson.email_resolution`.
3. For any paid source run, call `estimate_only=true` first.
4. If the tool returns `job_id`, poll `get_job_status` until completed/failed.
5. Report counts / cost only. Do not dump contact payloads.

## Table source
Maps-scraper style params, mutually exclusive with `rows`:

```
source_table: "client_peterson.email_resolution"
where: "candidate_email is null"
```

`source_table` accepts `schema.table`. The server pages 500 rows via a
security-definer RPC when the schema is not `public` (PostgREST only exposes
public). Never returns payloads. Writeback (default on) patches `wf_status`,
`wf_email`, `wf_email_status`, `wf_vendor`, `wf_updated_at` on the source table.
Omitted `map` auto-picks `owner_title` → title and `candidate_email` → email.

## Name + company (no domain)
Rows with first_name + last_name + company_name and no domain are tagged
`mode=name_company`. They skip getleads and Smartlead and enter at AI Ark →
LeadMagic → Prospeo → FullEnrich. `company_name` is sent to FullEnrich
verbatim. When a tier returns an email, its domain is written back for later
tiers.

## Tiers
getleads → Smartlead (included plan email finder) → AI Ark → LeadMagic →
Prospeo → FullEnrich.
Default max_tier is **leadmagic** (alias `lm`). Prospeo and FullEnrich do not
run unless you raise max_tier.

Smartlead uses the monthly finder allotment on the Smartlead plan. Credits are
checked via search-analytics; once they are spent the cascade falls through to
the paid tiers. It is name+domain email only — not a DM people search.

AI Ark is fully used on all three lanes (not people-only):
- people/DM: People Search by domain + ranked titles
- email: LinkedIn URL, AI Ark person id, name+domain, or phone →
  `POST /v2/people/export/single`
- cellphone: LinkedIn URL or name+domain → `POST /v2/people/mobile-phone-finder`

Cellphones also fall through to LeadMagic mobile-finder (and Prospeo if
max_tier allows). Input `phone` / `cellphone` / `mobile` is written as cellphone.

## Input row shape
domain (optional if name+company present), company_name, first_name, last_name,
title, email, linkedin_url, phone / cellphone / mobile, place_id, city, state.
"""

WHEN_TO_USE = """
Use this MCP when the user already has company domains (or known people) and
needs decision-maker names, work emails, and/or cellphones written to isolated
per-client Supabase tables (basco, peterson, or any new snake_case client_tag).

Do NOT use this MCP for Google Maps scraping, website crawling, permit data,
or Apify contact-info scrapes.
"""
