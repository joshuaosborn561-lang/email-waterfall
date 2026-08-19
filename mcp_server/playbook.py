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
1. Optional: `ensure_client({ client_tag, profile, target_titles })` for a new client.
2. Call `enrich_waterfall` with `rows`, `client_tag`, `need`, `max_tier`.
3. If the tool returns `job_id`, poll `get_job_status` until completed/failed.
4. Report counts only. Do not dump contact payloads.

## Tiers
getleads → AI Ark → LeadMagic → Prospeo → FullEnrich.
Default max_tier is fullenrich (alias `fe`). Cap earlier with max_tier if needed.

AI Ark is second on the email lane too: LinkedIn URL, AI Ark person id,
name+domain, or phone → verified work email. It is not skipped just because
a row already has a name.

## Input row shape
domain (required), company_name, first_name, last_name, title, email,
linkedin_url, phone, place_id, city, state.
"""

WHEN_TO_USE = """
Use this MCP when the user already has company domains (or known people) and
needs decision-maker names and/or work emails written to isolated per-client
Supabase tables (basco, peterson, or any new snake_case client_tag).

Do NOT use this MCP for Google Maps scraping, website crawling, permit data,
or Apify contact-info scrapes.
"""
