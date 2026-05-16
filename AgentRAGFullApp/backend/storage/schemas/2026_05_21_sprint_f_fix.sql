-- Sprint F hotfix · lexai_has_module retornaba false para módulos no-core
-- Bug: coalesce(is_core, ...) → is_core es NOT NULL, así que cuando is_core=false
-- el coalesce se quedaba ahí y nunca evaluaba override ni plan_value.
-- Fix: usar CASE WHEN para que el corto-circuito sólo aplique cuando is_core=true.

CREATE OR REPLACE FUNCTION public.lexai_has_module(p_firm_id uuid, p_module_key text)
 RETURNS boolean
 LANGUAGE sql
 STABLE
AS $body$
  with module_meta as (
    select is_core, kill_switch_default from modules where key = p_module_key
  ),
  override_v as (
    select enabled from firm_module_overrides
     where firm_id = p_firm_id and module_key = p_module_key
       and (expires_at is null or expires_at > now())
     limit 1
  ),
  plan_value as (
    select pm.enabled
      from plan_modules pm
      join firm_subscriptions s on s.plan_code = pm.plan_code
     where s.firm_id = p_firm_id and pm.module_key = p_module_key
     limit 1
  )
  select case
    when (select is_core from module_meta) then true
    else coalesce(
      (select enabled from override_v),
      (select enabled from plan_value),
      (select kill_switch_default from module_meta),
      false
    )
  end;
$body$;
