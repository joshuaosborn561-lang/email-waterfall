"""DM / work-email enrichment waterfall.

Tiers (fixed, no Maps, no website crawl, no Apify):
  getleads → Smartlead (plan email finder) → AI Ark → LeadMagic → Prospeo → FullEnrich

AI Ark is third on BOTH lanes after the included Smartlead allotment is used:
  people/DM: People Search by domain
  email: LinkedIn URL / person id / name+domain / phone → export/single

Also fills cellphone: AI Ark mobile-phone-finder (LinkedIn or name+domain),
then LeadMagic mobile-finder, then Prospeo if max_tier allows.

On need='phone' only, every candidate number (input or vendor) is checked
with Veriphone GET /v2/verify. Only phone_type=mobile is written as cellphone.
Landline / voip / invalid fall through to the next finder. The number and
Veriphone phone_type are written back to the source table as wf_phone /
wf_phone_type, and to {client}_*contacts.line_type. need='both' / 'email'
skip Veriphone unless verify_only=True (check numbers we already have; no
finder spend).

`need` is an allowlist of vendor capabilities (email / phone / people), applied
before any vendor HTTP call. need='email' must not call phone endpoints.
AI Ark search-then-export is one attempt and two vendor_calls.

Writes to public.{client}_companies / public.{client}_contacts.
"""
