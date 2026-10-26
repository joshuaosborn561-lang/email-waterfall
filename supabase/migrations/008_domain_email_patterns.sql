-- 008: known company email formats for the pattern tier (project azpapwtnrbzywlnxxecz).
--
-- public.domain_email_patterns may already exist from an older Hunter cache
-- (primary key (domain) only, columns organization / sighted_emails / ...).
-- This migration keeps any legacy columns, adds the pattern-tier columns and
-- moves the primary key to (domain, pattern). Safe to re-run.

create table if not exists public.domain_email_patterns (
  domain text not null,
  email_domain text not null,
  pattern text not null,
  n_verified integer not null default 0,
  is_catch_all boolean not null default false,
  source text,
  updated_at timestamptz not null default now(),
  primary key (domain, pattern)
);

alter table public.domain_email_patterns add column if not exists email_domain text;
alter table public.domain_email_patterns add column if not exists n_verified integer not null default 0;
alter table public.domain_email_patterns add column if not exists is_catch_all boolean not null default false;
alter table public.domain_email_patterns add column if not exists source text;
alter table public.domain_email_patterns add column if not exists updated_at timestamptz not null default now();
update public.domain_email_patterns set email_domain = domain where email_domain is null;
alter table public.domain_email_patterns alter column email_domain set not null;
delete from public.domain_email_patterns where pattern is null;
alter table public.domain_email_patterns alter column pattern set not null;

do $$
declare
  pk_name text;
  pk_def text;
begin
  select conname, pg_get_constraintdef(oid) into pk_name, pk_def
  from pg_constraint
  where conrelid = 'public.domain_email_patterns'::regclass and contype = 'p';
  if pk_def is distinct from 'PRIMARY KEY (domain, pattern)' then
    if pk_name is not null then
      execute format('alter table public.domain_email_patterns drop constraint %I', pk_name);
    end if;
    alter table public.domain_email_patterns add primary key (domain, pattern);
  end if;
end $$;

create index if not exists domain_email_patterns_domain_idx on public.domain_email_patterns (domain);

-- Where verified ("passed") emails come from. Add a row to add a source.
-- pass_condition is a SQL boolean over the source table's columns.
create table if not exists public.domain_email_pattern_sources (
  source_schema text not null,
  source_table text not null,
  email_column text not null default 'email',
  pass_condition text not null default 'true',
  enabled boolean not null default true,
  note text,
  primary key (source_schema, source_table)
);

-- Sources are registered per project (not in this file), e.g.
--   insert into public.domain_email_pattern_sources (source_schema, source_table)
--   values ('client_x', 'ver_1025');

-- Same rules as email_waterfall.names / vendors.pattern.build().
create extension if not exists unaccent with schema extensions;

-- plpgsql (not SQL) so the plans are cached; SQL-language versions are
-- re-planned per row and far too slow over the contacts tables.
create or replace function public.ew_name_tokens(p text)
returns text[] language plpgsql immutable parallel safe as $$
declare
  out_t text[] := '{}';
  tok text;
begin
  foreach tok in array regexp_split_to_array(lower(extensions.unaccent(coalesce(p, ''))), '[\s,]+') loop
    tok := regexp_replace(tok, '[^a-z]', '', 'g');
    if tok <> '' and tok not in ('dr','mr','mrs','ms','miss','mx','prof','rev','sir') then
      out_t := out_t || tok;
    end if;
  end loop;
  return out_t;
end $$;

create or replace function public.ew_norm_first(p text)
returns text language plpgsql immutable parallel safe as $$
declare
  toks text[] := public.ew_name_tokens(p);
  tok text;
begin
  foreach tok in array toks loop
    if length(tok) >= 2 then
      return tok;
    end if;
  end loop;
  return coalesce(toks[1], '');
end $$;

create or replace function public.ew_norm_last(p text)
returns text language plpgsql immutable parallel safe as $$
declare
  toks text[] := public.ew_name_tokens(p);
  i int;
begin
  for i in reverse coalesce(array_length(toks, 1), 0) .. 1 loop
    if toks[i] not in ('jr','sr','ii','iii','iv','md','phd','rn','dnp','mba','fache','cpa','do','np') then
      return toks[i];
    end if;
  end loop;
  return '';
end $$;

create or replace function public.ew_email_pattern(p_local text, p_first text, p_last text)
returns text language plpgsql immutable parallel safe as $$
declare
  f text := public.ew_norm_first(p_first);
  l text := public.ew_norm_last(p_last);
begin
  if length(f) < 2 or length(l) < 2 then return null; end if;
  return case
    when p_local = f || '.' || l then 'first.last'
    when p_local = f || '_' || l then 'first_last'
    when p_local = f || '-' || l then 'first-last'
    when p_local = left(f, 1) || '.' || l then 'f.last'
    when p_local = l || '.' || f then 'last.first'
    when p_local = f || l then 'firstlast'
    when p_local = left(f, 1) || l then 'flast'
    when p_local = f || left(l, 1) then 'firstl'
    when p_local = l || left(f, 1) then 'lastf'
    when p_local = f then 'first'
    else null
  end;
end $$;

-- Rebuild counts from verified emails we already hold. Never flips
-- is_catch_all back to false. Returns rows upserted.
create or replace function public.refresh_domain_email_patterns()
returns integer
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  src record;
  tbl record;
  ver_sql text := '';
  contact_sql text := '';
  email_expr text;
  n integer := 0;
begin
  for src in
    select s.* from public.domain_email_pattern_sources s
    where s.enabled
      and to_regclass(format('%I.%I', s.source_schema, s.source_table)) is not null
  loop
    ver_sql := ver_sql || case when ver_sql = '' then '' else ' union ' end
      || format('select lower(trim(%I::text)) as email from %I.%I where %s',
                src.email_column, src.source_schema, src.source_table, src.pass_condition);
  end loop;
  if ver_sql = '' then
    return 0;
  end if;

  for tbl in
    select c.table_name,
           bool_or(c.column_name = 'wf_email') as has_wf,
           bool_or(c.column_name = 'email') as has_email
    from information_schema.columns c
    where c.table_schema = 'public' and c.table_name like '%\_wf\_contacts'
    group by c.table_name
    having bool_or(c.column_name = 'first_name')
       and bool_or(c.column_name = 'last_name')
       and bool_or(c.column_name = 'domain')
       and (bool_or(c.column_name = 'wf_email') or bool_or(c.column_name = 'email'))
  loop
    email_expr := case
      when tbl.has_wf and tbl.has_email then 'coalesce(nullif(trim(wf_email), ''''), nullif(trim(email), ''''))'
      when tbl.has_wf then 'nullif(trim(wf_email), '''')'
      else 'nullif(trim(email), '''')'
    end;
    contact_sql := contact_sql || case when contact_sql = '' then '' else ' union all ' end
      || format('select %s as email, first_name, last_name, domain, %L as src from public.%I',
                email_expr, tbl.table_name, tbl.table_name);
  end loop;
  if contact_sql = '' then
    return 0;
  end if;

  execute format($q$
    with ver as (%s),
    c as (
      select distinct lower(x.email) as email, x.first_name, x.last_name,
             regexp_replace(lower(trim(x.domain)), '^www\.', '') as domain
      from (%s) x
      where x.email is not null and x.email like '%%@%%'
        and coalesce(x.first_name, '') <> '' and coalesce(x.last_name, '') <> ''
        and coalesce(x.domain, '') <> ''
    ),
    p as (
      select c.domain,
             split_part(c.email, '@', 2) as email_domain,
             public.ew_email_pattern(split_part(c.email, '@', 1), c.first_name, c.last_name) as pattern,
             c.email
      from c join ver v on v.email = c.email
    ),
    agg as (
      select domain, email_domain, pattern, count(distinct email)::int as n
      from p where pattern is not null
      group by 1, 2, 3
    ),
    best as (
      select distinct on (domain, pattern) domain, email_domain, pattern, n
      from agg order by domain, pattern, n desc
    )
    insert into public.domain_email_patterns as d (domain, email_domain, pattern, n_verified, source, updated_at)
    select domain, email_domain, pattern, n, 'refresh', now() from best
    on conflict (domain, pattern) do update
      set n_verified = greatest(d.n_verified, excluded.n_verified),
          email_domain = excluded.email_domain,
          updated_at = now()
  $q$, ver_sql, contact_sql);
  get diagnostics n = row_count;
  return n;
end;
$$;

-- Live tier hooks (called from vendors/pattern.py over PostgREST RPC).
create or replace function public.ew_bump_domain_pattern(p_domain text, p_email_domain text, p_pattern text)
returns void language sql security definer set search_path = public as $$
  insert into public.domain_email_patterns (domain, email_domain, pattern, n_verified, source, updated_at)
  values (lower(p_domain), lower(p_email_domain), p_pattern, 1, 'pattern_tier', now())
  on conflict (domain, pattern) do update
    set n_verified = public.domain_email_patterns.n_verified + 1, updated_at = now();
$$;

create or replace function public.ew_mark_domain_catch_all(p_domain text, p_email_domain text, p_pattern text)
returns void language sql security definer set search_path = public as $$
  insert into public.domain_email_patterns (domain, email_domain, pattern, is_catch_all, source, updated_at)
  values (lower(p_domain), lower(p_email_domain), p_pattern, true, 'pattern_tier', now())
  on conflict (domain, pattern) do update set is_catch_all = true, updated_at = now();
  update public.domain_email_patterns set is_catch_all = true, updated_at = now()
  where domain = lower(p_domain);
$$;

revoke all on function public.refresh_domain_email_patterns() from public, anon, authenticated;
revoke all on function public.ew_bump_domain_pattern(text, text, text) from public, anon, authenticated;
revoke all on function public.ew_mark_domain_catch_all(text, text, text) from public, anon, authenticated;

-- Nightly rebuild at 08:15 UTC (03:15 CT).
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.unschedule(jobid) from cron.job where jobname = 'refresh_domain_email_patterns';
    perform cron.schedule('refresh_domain_email_patterns', '15 8 * * *',
                          'select public.refresh_domain_email_patterns()');
  end if;
end $$;
