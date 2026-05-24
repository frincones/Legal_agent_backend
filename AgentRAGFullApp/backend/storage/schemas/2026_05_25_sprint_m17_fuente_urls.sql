-- ============================================================
-- LexAI · Sprint M17 · Fuente URLs garantizadas + HEAD validation
-- Migration date: 2026-05-25
-- Idempotent · additive
-- ============================================================

do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_name = 'citation_verifications' and column_name = 'fuente_url_original'
  ) then
    alter table citation_verifications add column fuente_url_original text;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'citation_verifications' and column_name = 'fuente_url_vigente'
  ) then
    alter table citation_verifications add column fuente_url_vigente text;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'citation_verifications' and column_name = 'url_validated_at'
  ) then
    alter table citation_verifications add column url_validated_at timestamptz;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'citation_verifications' and column_name = 'url_http_status'
  ) then
    alter table citation_verifications add column url_http_status int;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'verification_attempts' and column_name = 'fuente_url_validated'
  ) then
    alter table verification_attempts add column fuente_url_validated boolean default false;
  end if;
end $$;
