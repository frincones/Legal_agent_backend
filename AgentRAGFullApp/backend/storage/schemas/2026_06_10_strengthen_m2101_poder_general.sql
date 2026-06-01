-- M21.01 v1.1 · Refuerzo del SKILL poder_general detectado en testing manual:
--
-- BUG 1: agente citó Art. 74 CGP a pesar de estar en blacklist
-- BUG 2: documento usó "esta acta" en vez de "este instrumento"
-- IMPROVEMENT: prepend critical instructions al system_prompt
--
-- Idempotente · re-aplica UPDATE on conflict.

begin;

-- Update version + reinforce system_prompt with critical pre-emit instructions
update firm_skills set
    version = 2,
    updated_at = now(),
    system_prompt = E'# Poder General, Amplio y Suficiente — Colombia (v1.1)\n\n' ||
                    E'## ⚠️ INSTRUCCIONES CRÍTICAS PRE-EMIT (LEER PRIMERO)\n\n' ||
                    E'Antes de devolver el documento final al usuario:\n\n' ||
                    E'1. **CHECK BLACKLIST DE CITAS** — busca literalmente en el output:\n' ||
                    E'   - \"Art. 74 CGP\", \"art. 74 del Código General del Proceso\", \"Ley 1564/2012\" o \"Ley 1564 de 2012\"\n' ||
                    E'   - \"Ley 1996 de 2019\", \"Ley 1306 de 2009\"\n' ||
                    E'   Si encuentras CUALQUIERA de estas → ELIMÍNALA del cuerpo Y del footer de citas. Regenera la sección afectada SIN esa cita.\n\n' ||
                    E'2. **CHECK LÉXICO NOTARIAL** — reemplazar SIEMPRE:\n' ||
                    E'   - \"esta acta\" → \"este instrumento\" (un poder es instrumento, NO acta)\n' ||
                    E'   - \"la presente acta\" → \"el presente instrumento\"\n' ||
                    E'   - \"actuar de manera libre\" → \"obrando libre y espontáneamente\"\n' ||
                    E'   - \"manifestó hallarse\" + \"actuar\" → cambiar a \"manifestó encontrarse en pleno uso de sus facultades mentales, obrando libre y espontáneamente\"\n\n' ||
                    E'3. **CHECK ESTRUCTURA OBLIGATORIA** — las 12 facultades de disposición (Cláusula TERCERA) DEBEN estar enumeradas literalmente. No basta con decir \"y demás actos de disposición\".\n\n' ||
                    E'4. **CITAS VÁLIDAS ÚNICAS** para este tipo de documento:\n' ||
                    E'   - Art. 2142, 2155, 2158, 2163, 2189 del Código Civil (mandato)\n' ||
                    E'   - Decreto 960/1970 Arts. 12, 25, 38, 42 (Estatuto Notarial)\n' ||
                    E'   - Art. 196 Código de Comercio (si poderdante es PJ)\n' ||
                    E'   - Decreto 2148/1983 (Reglamento Notariado)\n' ||
                    E'   PROHIBIDO: cualquier artículo del CGP, Ley 1996/2019, Ley 1306/2009.\n\n' ||
                    E'---\n\n' ||
                    -- Resto del system_prompt original (preservado)
                    system_prompt
where firm_id is null
  and command = '/redactar/poder-general';

-- Validation
do $$
declare v_version int;
begin
  select version into v_version from firm_skills
   where firm_id is null and command = '/redactar/poder-general' and status='published'
   order by version desc limit 1;
  if v_version is null or v_version < 2 then
    raise exception 'M21.01 v1.1 strengthen FAILED: version=%', v_version;
  end if;
  raise notice 'M21.01 v1.1 strengthen OK: SKILL version=%', v_version;
end$$;

commit;
