-- Claim unfinished source rows with an expiring processing lease so
-- parallel workers and restarted jobs do not re-scan or double-spend.
-- Identifier-safe only. Project azpapwtnrbzywlnxxecz.

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
      'wf_status', 'wf_email', 'wf_email_status', 'wf_vendor', 'wf_updated_at',
      'wf_phone', 'wf_phone_type', 'wf_lease_until', 'wf_lease_owner'
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
    typ := CASE
      WHEN col LIKE '%\_at' ESCAPE '\' OR col LIKE '%\_until' ESCAPE '\' THEN 'timestamptz'
      ELSE 'text'
    END;
    EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS %I %s', sch, tbl, col, typ);
    added := added || col;
  END LOOP;
  PERFORM pg_notify('pgrst', 'reload schema');
  RETURN added;
END;
$function$;

CREATE OR REPLACE FUNCTION public.ew_claim_source(
  p_schema text,
  p_table text,
  p_filters jsonb DEFAULT '[]'::jsonb,
  p_columns text[] DEFAULT NULL,
  p_key_column text DEFAULT 'id',
  p_limit integer DEFAULT 1,
  p_owner text DEFAULT NULL,
  p_lease_seconds integer DEFAULT 900
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
  owner text;
  lease int;
  has_status boolean;
  has_lease boolean;
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  keycol := lower(regexp_replace(coalesce(p_key_column, 'id'), '[^a-z0-9_]', '', 'g'));
  lim := least(greatest(coalesce(p_limit, 1), 1), 50);
  owner := left(coalesce(p_owner, 'ew'), 80);
  lease := least(greatest(coalesce(p_lease_seconds, 900), 60), 3600);
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns c
    WHERE c.table_schema = sch AND c.table_name = tbl AND c.column_name = 'wf_status'
  ) INTO has_status;
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns c
    WHERE c.table_schema = sch AND c.table_name = tbl AND c.column_name = 'wf_lease_until'
  ) INTO has_lease;
  IF NOT has_status OR NOT has_lease THEN
    RAISE EXCEPTION 'source %.% missing wf_status / wf_lease_until (run ew_ensure_wf_writeback)', sch, tbl;
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
    IF col = '' OR col IN ('wf_status', 'wf_lease_until', 'wf_lease_owner') THEN
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

  -- Unfinished: never claimed, or a processing lease that expired.
  -- Found / title_miss / not_found are never reclaimed.
  clause := clause || $unfin$
    AND (
      wf_status IS NULL
      OR (
        wf_status = 'processing'
        AND (wf_lease_until IS NULL OR wf_lease_until < now())
      )
    )
  $unfin$;

  sql := format(
    'WITH claimed AS (
       UPDATE %I.%I AS s
          SET wf_status = %L,
              wf_lease_until = now() + make_interval(secs => %s),
              wf_lease_owner = %L
        WHERE s.%I IN (
          SELECT %I FROM %I.%I%s
          ORDER BY %I ASC
          LIMIT %s
          FOR UPDATE SKIP LOCKED
        )
       RETURNING %s
     )
     SELECT coalesce(jsonb_agg(to_jsonb(t)), ''[]''::jsonb) FROM claimed t',
    sch, tbl,
    'processing',
    lease,
    owner,
    keycol,
    keycol, sch, tbl, clause,
    keycol,
    lim,
    (SELECT string_agg(format('s.%I', c), ', ') FROM unnest(cols) AS c)
  );
  EXECUTE sql INTO result;
  RETURN coalesce(result, '[]'::jsonb);
END;
$function$;

CREATE OR REPLACE FUNCTION public.ew_release_claims(
  p_schema text,
  p_table text,
  p_key_column text DEFAULT 'id',
  p_owner text DEFAULT NULL,
  p_keys text[] DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  sch text;
  tbl text;
  keycol text;
  owner text;
  n int := 0;
BEGIN
  sch := lower(regexp_replace(coalesce(p_schema, 'public'), '[^a-z0-9_]', '', 'g'));
  tbl := lower(regexp_replace(coalesce(p_table, ''), '[^a-z0-9_]', '', 'g'));
  keycol := lower(regexp_replace(coalesce(p_key_column, 'id'), '[^a-z0-9_]', '', 'g'));
  owner := left(coalesce(p_owner, ''), 80);
  IF sch = '' OR tbl = '' OR to_regclass(format('%I.%I', sch, tbl)) IS NULL THEN
    RAISE EXCEPTION 'unknown source table %.%', sch, tbl;
  END IF;
  IF owner = '' AND (p_keys IS NULL OR coalesce(array_length(p_keys, 1), 0) = 0) THEN
    RETURN 0;
  END IF;
  IF p_keys IS NOT NULL AND coalesce(array_length(p_keys, 1), 0) > 0 THEN
    EXECUTE format(
      'UPDATE %I.%I SET wf_status = NULL, wf_lease_until = NULL, wf_lease_owner = NULL
        WHERE wf_status = %L
          AND %I::text = ANY (%L)
          AND (%L = '''' OR wf_lease_owner = %L)',
      sch, tbl, 'processing', keycol, p_keys, owner, owner
    );
  ELSE
    EXECUTE format(
      'UPDATE %I.%I SET wf_status = NULL, wf_lease_until = NULL, wf_lease_owner = NULL
        WHERE wf_status = %L AND wf_lease_owner = %L',
      sch, tbl, 'processing', owner
    );
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$function$;

GRANT EXECUTE ON FUNCTION public.ew_ensure_wf_writeback(text, text, text[]) TO service_role;
GRANT EXECUTE ON FUNCTION public.ew_claim_source(text, text, jsonb, text[], text, integer, text, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.ew_release_claims(text, text, text, text, text[]) TO service_role;

NOTIFY pgrst, 'reload schema';
