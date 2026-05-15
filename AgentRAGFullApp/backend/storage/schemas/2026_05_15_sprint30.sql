-- ================================================================
-- Sprint 30 · Rigor entitlements · plan free
-- ================================================================
-- Motivación: el seed del sprint 25 era demasiado generoso para el
-- plan 'free' (trial 14 días). Incluía voice_agent, ai_chat, invoices,
-- time_entries y expenses, que son features de valor de Starter/Pro.
--
-- Cambio: forzar enabled=false para esos módulos en plan_modules
-- del plan 'free'. Idempotente · si admin ya los apagó vía /saas/modules,
-- el UPDATE no causa daño.
--
-- Aditivo: no toca otros planes ni otros módulos.
-- ================================================================

update plan_modules
   set enabled = false
 where plan_code = 'free'
   and module_key in (
     'voice_agent',     -- premium · OpenAI Realtime API · costoso
     'ai_chat',         -- premium · LLM unlimited · Starter+
     'invoices',        -- billing · feature de Pro+
     'time_entries',    -- billing · feature de Pro+
     'expenses'         -- billing · feature de Pro+
   );

-- Verificación
select 'free plan modules (only enabled=true)' as info, module_key
  from plan_modules
 where plan_code = 'free' and enabled = true
 order by module_key;
