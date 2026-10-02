-- Phone writeback + Veriphone line type.
-- Source tables get wf_phone / wf_phone_type via ew_ensure_wf_writeback.
-- Each client contacts table gets line_type so a later save can update it.

ALTER TABLE public.basco_contacts ADD COLUMN IF NOT EXISTS line_type text;
ALTER TABLE public.peterson_contacts ADD COLUMN IF NOT EXISTS line_type text;

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
      'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS line_type text',
      r.table_name
    );
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.ew_ensure_contact_columns(
  p_table text,
  columns text[] DEFAULT ARRAY['line_type']
) RETURNS text[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  tbl text;
  col text;
  added text[] := ARRAY[]::text[];
  wanted text[] := coalesce(columns, ARRAY['line_type']);
BEGIN
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  IF tbl = '' OR to_regclass(format('public.%I', tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown contacts table %', tbl;
  END IF;
  IF tbl NOT LIKE '%_contacts' THEN
    RAISE EXCEPTION 'ew_ensure_contact_columns only accepts *contacts tables';
  END IF;
  FOREACH col IN ARRAY wanted
  LOOP
    col := lower(regexp_replace(col, '[^a-z0-9_]', '', 'g'));
    IF col = '' THEN
      CONTINUE;
    END IF;
    IF EXISTS (
      SELECT 1 FROM information_schema.columns c
      WHERE c.table_schema = 'public' AND c.table_name = tbl AND c.column_name = col
    ) THEN
      CONTINUE;
    END IF;
    EXECUTE format(
      'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS %I text',
      tbl,
      col
    );
    added := added || col;
  END LOOP;
  PERFORM pg_notify('pgrst', 'reload schema');
  RETURN added;
END;
$function$;

CREATE OR REPLACE FUNCTION public.ew_ensure_wf_writeback(
  schema_name text,
  table_name text,
  columns text[] DEFAULT NULL
) RETURNS text[]
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
  col text;
  typ text;
  added text[] := ARRAY[]::text[];
  wanted text[] := coalesce(
    columns,
    ARRAY[
      'wf_status',
      'wf_email',
      'wf_email_status',
      'wf_vendor',
      'wf_updated_at',
      'wf_phone',
      'wf_phone_type'
    ]
  );
BEGIN
  sch := lower(regexp_replace(coalesce(schema_name, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(table_name, ''), '[^a-z0-9_]', '', 'g'));
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;
  FOREACH col IN ARRAY wanted
  LOOP
    col := lower(regexp_replace(col, '[^a-z0-9_]', '', 'g'));
    IF col = '' THEN
      CONTINUE;
    END IF;
    IF EXISTS (
      SELECT 1 FROM information_schema.columns c
      WHERE c.table_schema = sch AND c.table_name = tbl AND c.column_name = col
    ) THEN
      CONTINUE;
    END IF;
    typ := CASE WHEN col LIKE '%\_at' ESCAPE '\' THEN 'timestamptz' ELSE 'text' END;
    EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS %I %s', sch, tbl, col, typ);
    added := added || col;
  END LOOP;
  PERFORM pg_notify('pgrst', 'reload schema');
  RETURN added;
END;
$function$;

GRANT EXECUTE ON FUNCTION public.ew_ensure_contact_columns(text, text[]) TO service_role;
GRANT EXECUTE ON FUNCTION public.ew_ensure_wf_writeback(text, text, text[]) TO service_role;

NOTIFY pgrst, 'reload schema';
