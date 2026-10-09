-- Person-key unique index so re-runs upsert null-email contacts instead of
-- inserting a duplicate. Not applied by the service.
--
-- Prerequisite: dedupe existing null-email person duplicates first
-- (salesglider_trades_wf_contacts and deep_roots_wf_contacts are known
-- dirty). Creating the unique index on a table that still has duplicates
-- will fail. Do not touch dl_status / sg_exclude / skip_* columns.

ALTER TABLE public.basco_contacts ADD COLUMN IF NOT EXISTS first_name_key text;
ALTER TABLE public.basco_contacts ADD COLUMN IF NOT EXISTS last_name_key text;
ALTER TABLE public.peterson_contacts ADD COLUMN IF NOT EXISTS first_name_key text;
ALTER TABLE public.peterson_contacts ADD COLUMN IF NOT EXISTS last_name_key text;

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT c.table_name
    FROM information_schema.tables c
    WHERE c.table_schema = 'public'
      AND c.table_type = 'BASE TABLE'
      AND (
        c.table_name LIKE '%\_wf_contacts' ESCAPE '\'
        OR c.table_name IN ('basco_contacts', 'peterson_contacts')
      )
  LOOP
    EXECUTE format(
      'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS first_name_key text',
      r.table_name
    );
    EXECUTE format(
      'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS last_name_key text',
      r.table_name
    );
    EXECUTE format(
      'UPDATE public.%I SET
         first_name_key = nullif(lower(trim(first_name)), ''''),
         last_name_key = nullif(lower(trim(last_name)), '''')
       WHERE first_name_key IS NULL OR last_name_key IS NULL',
      r.table_name
    );
    EXECUTE format(
      'CREATE UNIQUE INDEX IF NOT EXISTS %I ON public.%I
         (client_tag, domain, first_name_key, last_name_key)',
      r.table_name || '_person_uidx',
      r.table_name
    );
    -- salesglider_trades_wf_contacts historically lacked (domain, email).
    EXECUTE format(
      'CREATE UNIQUE INDEX IF NOT EXISTS %I ON public.%I (domain, email)',
      r.table_name || '_domain_email_uidx',
      r.table_name
    );
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.ew_ensure_contact_person_key(
  p_table text
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  tbl text;
BEGIN
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  IF tbl = '' OR to_regclass(format('public.%I', tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown contacts table %', tbl;
  END IF;
  IF tbl NOT LIKE '%_contacts' THEN
    RAISE EXCEPTION 'ew_ensure_contact_person_key only accepts *contacts tables';
  END IF;
  EXECUTE format(
    'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS first_name_key text',
    tbl
  );
  EXECUTE format(
    'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS last_name_key text',
    tbl
  );
  EXECUTE format(
    'CREATE UNIQUE INDEX IF NOT EXISTS %I ON public.%I
       (client_tag, domain, first_name_key, last_name_key)',
    tbl || '_person_uidx',
    tbl
  );
  EXECUTE format(
    'CREATE UNIQUE INDEX IF NOT EXISTS %I ON public.%I (domain, email)',
    tbl || '_domain_email_uidx',
    tbl
  );
  PERFORM pg_notify('pgrst', 'reload schema');
  RETURN tbl;
END;
$function$;

GRANT EXECUTE ON FUNCTION public.ew_ensure_contact_person_key(text) TO service_role;

NOTIFY pgrst, 'reload schema';
