-- Read/write source tables in any schema (e.g. client_peterson.email_resolution)
-- without exposing that schema on the PostgREST API. Identifier-safe only.

CREATE OR REPLACE FUNCTION public.ew_source_columns(
  p_schema text,
  p_table text
) RETURNS text[]
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
  cols text[];
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;
  SELECT array_agg(c.column_name::text ORDER BY c.ordinal_position)
    INTO cols
  FROM information_schema.columns c
  WHERE c.table_schema = sch AND c.table_name = tbl;
  RETURN coalesce(cols, ARRAY[]::text[]);
END;
$function$;

CREATE OR REPLACE FUNCTION public.ew_read_source(
  p_schema text,
  p_table text,
  p_filters jsonb DEFAULT '[]'::jsonb,
  p_columns text[] DEFAULT NULL,
  p_key_column text DEFAULT 'id',
  p_after text DEFAULT NULL,
  p_limit integer DEFAULT 500
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
  keycol text;
  cols text[];
  filt jsonb;
  clause text := ' WHERE true';
  col text;
  op text;
  val text;
  sql text;
  result jsonb;
  lim int;
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  keycol := lower(regexp_replace(coalesce(p_key_column, 'id'), '[^a-z0-9_]', '', 'g'));
  lim := least(greatest(coalesce(p_limit, 500), 1), 500);
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;

  IF p_columns IS NULL OR coalesce(array_length(p_columns, 1), 0) = 0 THEN
    cols := ARRAY[keycol];
  ELSE
    SELECT array_agg(lower(regexp_replace(c, '[^a-z0-9_]', '', 'g')))
      INTO cols
    FROM unnest(p_columns) AS c
    WHERE c IS NOT NULL AND btrim(c) <> '';
    IF cols IS NULL THEN
      cols := ARRAY[keycol];
    ELSIF NOT keycol = ANY (cols) THEN
      cols := cols || keycol;
    END IF;
  END IF;

  FOR filt IN SELECT * FROM jsonb_array_elements(coalesce(p_filters, '[]'::jsonb))
  LOOP
    col := lower(regexp_replace(coalesce(filt->>'col', ''), '[^a-z0-9_]', '', 'g'));
    op := lower(coalesce(filt->>'op', ''));
    val := filt->>'value';
    IF col = '' THEN
      CONTINUE;
    END IF;
    IF op IN ('is.null', 'is null') THEN
      clause := clause || format(' AND %I IS NULL', col);
    ELSIF op IN ('not.is.null', 'is not null') THEN
      clause := clause || format(' AND %I IS NOT NULL', col);
    ELSIF op IN ('eq', '=') THEN
      clause := clause || format(' AND %I = %L', col, val);
    ELSIF op IN ('neq', '!=', '<>') THEN
      clause := clause || format(' AND %I <> %L', col, val);
    ELSE
      RAISE EXCEPTION 'unsupported filter op: %', op;
    END IF;
  END LOOP;

  IF p_after IS NOT NULL AND btrim(p_after) <> '' THEN
    clause := clause || format(' AND %I::text > %L', keycol, p_after);
  END IF;

  sql := format(
    'SELECT coalesce(jsonb_agg(to_jsonb(t)), ''[]''::jsonb) FROM (SELECT %s FROM %I.%I%s ORDER BY %I ASC LIMIT %s) t',
    (SELECT string_agg(format('%I', c), ', ') FROM unnest(cols) AS c),
    sch,
    tbl,
    clause,
    keycol,
    lim
  );
  EXECUTE sql INTO result;
  RETURN coalesce(result, '[]'::jsonb);
END;
$function$;

CREATE OR REPLACE FUNCTION public.ew_patch_source(
  p_schema text,
  p_table text,
  p_key_column text,
  p_key text,
  p_fields jsonb
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
  keycol text;
  col text;
  sets text := '';
  val jsonb;
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  keycol := lower(regexp_replace(coalesce(p_key_column, 'id'), '[^a-z0-9_]', '', 'g'));
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;
  IF p_key IS NULL OR btrim(p_key) = '' THEN
    RETURN;
  END IF;
  FOR col, val IN SELECT key, value FROM jsonb_each(coalesce(p_fields, '{}'::jsonb))
  LOOP
    col := lower(regexp_replace(col, '[^a-z0-9_]', '', 'g'));
    IF col = '' THEN
      CONTINUE;
    END IF;
    IF sets <> '' THEN
      sets := sets || ', ';
    END IF;
    IF val IS NULL OR val = 'null'::jsonb THEN
      sets := sets || format('%I = NULL', col);
    ELSE
      sets := sets || format('%I = %L', col, trim(both '"' from val::text));
    END IF;
  END LOOP;
  IF sets = '' THEN
    RETURN;
  END IF;
  EXECUTE format('UPDATE %I.%I SET %s WHERE %I::text = %L', sch, tbl, sets, keycol, p_key);
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
    ARRAY['wf_status', 'wf_email', 'wf_email_status', 'wf_vendor', 'wf_updated_at']
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

GRANT EXECUTE ON FUNCTION public.ew_source_columns(text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.ew_read_source(text, text, jsonb, text[], text, text, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.ew_patch_source(text, text, text, text, jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.ew_ensure_wf_writeback(text, text, text[]) TO service_role;

NOTIFY pgrst, 'reload schema';
