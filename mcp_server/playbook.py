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
- **basco** (Carlos) — franchise new-car dealership rooftops near Clifton, NJ.
  Ranked titles: Service Director → Fixed Ops Director → Service Manager →
  Warranty Manager → Director/VP of Service → GM / Dealer Principal (fallback).
  Writes `public.basco_companies` / `public.basco_contacts`.
- **peterson** (Kyle) — commercial roofing / GCs / PMs in Dallas-Fort Worth.
  Titles: Owner, Founder, Principal, President, Partner, CEO, VP, Director, GM.
  Writes `public.peterson_companies` / `public.peterson_contacts`.

Never omit `client_tag`. Never write to a shared contacts table.

## Default flow
1. Call `health` if vendor keys or Supabase might be missing.
2. Call `enrich_waterfall` with `rows` (JSON list of domain objects), `client_tag`,
   `need` (`dm` | `email` | `both`), `max_tier` (default `fullenrich`).
3. If the tool returns `job_id`, poll `get_job_status` until completed/failed.
4. Report counts only: rows_in, dms_found, emails_found, companies_upserted,
   contacts_written, tier_breakdown. Do not dump contact payloads.

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
needs decision-maker names and/or work emails written to Basco or Peterson
Supabase tables.

Do NOT use this MCP for Google Maps scraping, website crawling, permit data,
or Apify contact-info scrapes.
"""
