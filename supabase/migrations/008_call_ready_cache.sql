-- Per-person cache for call-ready enrichment (phone, LinkedIn, ICP, DM).
-- person_key = sha256(email | linkedin | name|domain)[:32]. No lead PII in the key.
CREATE TABLE IF NOT EXISTS public.ew_call_ready_cache (
  person_key text PRIMARY KEY,
  client_tag text NOT NULL,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.ew_call_ready_cache ENABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS ew_call_ready_cache_client_idx ON public.ew_call_ready_cache (client_tag);
