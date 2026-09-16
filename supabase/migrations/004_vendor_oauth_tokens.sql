-- OAuth tokens for vendors that have no API key (getleads MCP).
-- Service role only. Anon / authenticated must not read refresh tokens.

CREATE TABLE IF NOT EXISTS public.ew_vendor_oauth_tokens (
  vendor text PRIMARY KEY,
  client_id text,
  refresh_token text,
  access_token text,
  access_expires_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.ew_vendor_oauth_tokens ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.ew_vendor_oauth_tokens FROM PUBLIC;
REVOKE ALL ON TABLE public.ew_vendor_oauth_tokens FROM anon;
REVOKE ALL ON TABLE public.ew_vendor_oauth_tokens FROM authenticated;
GRANT ALL ON TABLE public.ew_vendor_oauth_tokens TO service_role;
