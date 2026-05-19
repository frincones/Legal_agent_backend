-- Sprint P0 · Persona "Dr./Dra. LexAI v1" — 7 tablas + RLS + función ensamblado
-- ============================================================================
-- ADR-007 Fase 0 · TASK-F0-01 a TASK-F0-04
-- Todas las sentencias son idempotentes (CREATE ... IF NOT EXISTS, etc.)
-- NO hace drop de nada. NO toca tablas existentes.
-- ============================================================================

-- ============================================================================
-- 1. TABLA: agent_personas
--    Raíz de identidad. firm_id NULL = sistema default compartido entre firmas.
-- ============================================================================

create table if not exists agent_personas (
  id          uuid primary key default gen_random_uuid(),
  firm_id     uuid null references firms(id) on delete cascade,
  slug        text not null,
  name        text not null,
  identity_md text not null default '',
  version     int  not null default 1,
  is_active   bool not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- UNIQUE sobre (firm_id, slug, version).
-- En Postgres, dos filas con firm_id IS NULL no colisionan por la regla NULLS≠NULLS,
-- así que usamos un índice único parcial para el espacio system (firm_id IS NULL)
-- y un índice único normal para el espacio de firma.
create unique index if not exists idx_agent_personas_system_slug_version
  on agent_personas (slug, version)
  where firm_id is null;

create unique index if not exists idx_agent_personas_firm_slug_version
  on agent_personas (firm_id, slug, version)
  where firm_id is not null;

create index if not exists idx_agent_personas_firm_active
  on agent_personas (firm_id, is_active);

comment on table agent_personas is
  'Sprint P0 · Identidades de persona del agente LexAI. firm_id IS NULL = default del sistema.';

-- RLS -----------------------------------------------------------------------
alter table agent_personas enable row level security;

drop policy if exists "agent_personas_tenant_read" on agent_personas;
create policy "agent_personas_tenant_read" on agent_personas
  for select
  using (
    firm_id is null
    or firm_id = (auth.jwt() ->> 'firm_id')::uuid
  );

-- Escritura solo vía service role (RPC security definer); abogados no escriben directo.

-- ============================================================================
-- 2. TABLA: personality_modules
--    Bloques S1-S10 (y skill_doctrine) ligados a una persona.
-- ============================================================================

create table if not exists personality_modules (
  id         uuid primary key default gen_random_uuid(),
  persona_id uuid not null references agent_personas(id) on delete cascade,
  type       text not null check (type in (
               'identity','tone','domain','output','safety','tools',
               'channel','refusal','recovery','examples',
               'skill_doctrine'
             )),
  skill_slug text null,  -- solo para type='skill_doctrine', ej. '/ask'
  order_index int not null default 0,
  title      text not null,
  body_md    text not null,
  enabled    bool not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  unique (persona_id, type, order_index)
);

create index if not exists idx_personality_modules_persona
  on personality_modules (persona_id, enabled);

comment on table personality_modules is
  'Sprint P0 · Módulos de personalidad S1-S10 asociados a una agent_persona.';

-- RLS -----------------------------------------------------------------------
alter table personality_modules enable row level security;

drop policy if exists "personality_modules_tenant_read" on personality_modules;
create policy "personality_modules_tenant_read" on personality_modules
  for select
  using (
    exists (
      select 1 from agent_personas ap
      where ap.id = personality_modules.persona_id
        and (ap.firm_id is null or ap.firm_id = (auth.jwt() ->> 'firm_id')::uuid)
    )
  );

-- ============================================================================
-- 3. TABLA: output_styles
--    Plantillas de estilo de salida (system o por firma).
-- ============================================================================

create table if not exists output_styles (
  id          uuid primary key default gen_random_uuid(),
  slug        text not null,
  name        text not null,
  template_md text not null,
  is_system   bool not null default false,
  firm_id     uuid null references firms(id) on delete cascade,

  -- UNIQUE: dentro de una firma (o dentro del espacio system) el slug es único
  unique nulls not distinct (firm_id, slug)
);

create index if not exists idx_output_styles_firm
  on output_styles (firm_id);

comment on table output_styles is
  'Sprint P0 · Plantillas de estilo de salida del agente. is_system=true → visibles para todas las firmas.';

-- RLS -----------------------------------------------------------------------
alter table output_styles enable row level security;

drop policy if exists "output_styles_tenant_read" on output_styles;
create policy "output_styles_tenant_read" on output_styles
  for select
  using (
    is_system = true
    or firm_id is null
    or firm_id = (auth.jwt() ->> 'firm_id')::uuid
  );

-- ============================================================================
-- 4. TABLA: firm_personality_overrides
--    Override de firma: qué persona usa, qué módulos desactiva, append extra.
-- ============================================================================

create table if not exists firm_personality_overrides (
  firm_id          uuid primary key references firms(id) on delete cascade,
  persona_id       uuid not null references agent_personas(id),
  output_style_id  uuid null references output_styles(id),
  system_append_md text null,
  disabled_modules text[] not null default '{}',
  updated_at       timestamptz not null default now()
);

comment on table firm_personality_overrides is
  'Sprint P0 · Override de personalidad por firma: persona activa, módulos desactivados, append D1.';

-- RLS -----------------------------------------------------------------------
alter table firm_personality_overrides enable row level security;

drop policy if exists "firm_personality_overrides_tenant" on firm_personality_overrides;
create policy "firm_personality_overrides_tenant" on firm_personality_overrides
  for all
  using (firm_id = (auth.jwt() ->> 'firm_id')::uuid);

-- ============================================================================
-- 5. TABLA: user_personality_preferences
--    Preferencias de usuario: tono, brevedad, formalidad, idioma.
-- ============================================================================

create table if not exists user_personality_preferences (
  user_id   uuid primary key references auth.users(id) on delete cascade,
  firm_id   uuid not null references firms(id) on delete cascade,
  tone      text not null default 'usted' check (tone in ('usted','tu')),
  brevity   text not null default 'normal' check (brevity in ('corto','normal','detallado')),
  formality text not null default 'formal' check (formality in ('formal','semi-formal')),
  language  text not null default 'es-CO',
  updated_at timestamptz not null default now()
);

create index if not exists idx_user_prefs_firm
  on user_personality_preferences (firm_id);

comment on table user_personality_preferences is
  'Sprint P0 · Preferencias de personalidad por usuario (capa D2).';

-- RLS -----------------------------------------------------------------------
alter table user_personality_preferences enable row level security;

drop policy if exists "user_personality_preferences_own" on user_personality_preferences;
create policy "user_personality_preferences_own" on user_personality_preferences
  for all
  using (user_id = auth.uid());

-- ============================================================================
-- 6. TABLA: agent_personality_versions  (audit log)
--    Snapshot de cada prompt ensamblado único (deduplicado por checksum).
-- ============================================================================

create table if not exists agent_personality_versions (
  id                     uuid primary key default gen_random_uuid(),
  firm_id                uuid null references firms(id) on delete cascade,
  -- null = versión generada para la persona system default (sin firma específica)
  persona_id             uuid not null references agent_personas(id),
  checksum               text not null,
  system_prompt_snapshot text not null,
  created_at             timestamptz not null default now(),

  unique (checksum)
);

create index if not exists idx_apv_firm_created
  on agent_personality_versions (firm_id, created_at desc);

comment on table agent_personality_versions is
  'Sprint P0 · Audit log de versiones de system prompt ensamblado. 1 fila por checksum único.';

-- RLS -----------------------------------------------------------------------
alter table agent_personality_versions enable row level security;

drop policy if exists "agent_personality_versions_admin_read" on agent_personality_versions;
create policy "agent_personality_versions_admin_read" on agent_personality_versions
  for select
  using (
    firm_id is null
    or firm_id = (auth.jwt() ->> 'firm_id')::uuid
  );

-- Sin escritura directa; solo la RPC (security definer) inserta.

-- ============================================================================
-- 7. TABLA: session_personality_overrides  (ephemeral, TTL 24h)
--    Override de sesión: instrucciones adicionales que expiran.
-- ============================================================================

create table if not exists session_personality_overrides (
  session_id text primary key,
  firm_id    uuid not null references firms(id) on delete cascade,
  append_md  text not null,
  expires_at timestamptz not null default (now() + interval '24 hours')
);

create index if not exists idx_spo_firm
  on session_personality_overrides (firm_id);

create index if not exists idx_spo_expires
  on session_personality_overrides (expires_at);

comment on table session_personality_overrides is
  'Sprint P0 · Override efímero de sesión (capa D3). TTL 24h; cron limpia filas expiradas.';

-- RLS -----------------------------------------------------------------------
alter table session_personality_overrides enable row level security;

drop policy if exists "session_personality_overrides_tenant" on session_personality_overrides;
create policy "session_personality_overrides_tenant" on session_personality_overrides
  for all
  using (firm_id = (auth.jwt() ->> 'firm_id')::uuid);

-- ============================================================================
-- 8. FUNCIÓN: lexai_assemble_system_prompt
--    Ensambla el system prompt a partir de las capas estáticas y dinámicas.
--    Implementa los 9 pasos del ADR-007 §2.2 completos (Fase 0 real, no stub).
--    SECURITY DEFINER para poder leer personality_modules de otras firmas
--    cuando la persona es la system default (firm_id IS NULL).
-- ============================================================================

create or replace function lexai_assemble_system_prompt(
  p_firm_id    uuid,
  p_user_id    uuid,
  p_channel    text,
  p_skill      text    default null,
  p_session_id text    default null
)
returns table (
  system_prompt text,
  version_id    uuid,
  checksum      text
)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_persona         agent_personas%rowtype;
  v_fpo             firm_personality_overrides%rowtype;
  v_has_fpo         bool := false;
  v_disabled        text[] := '{}';
  v_modules         personality_modules[];
  v_mod             personality_modules;
  v_parts           text[] := '{}';
  v_body            text;
  v_prompt          text;
  v_checksum        text;
  v_version_id      uuid;
  v_user_tone       text;
  v_user_brevity    text;
  v_user_language   text;
  v_session_append  text;
  v_firm_append     text;
  v_user_pref_line  text;

  -- Voice module types (S10 examples excluded from voice; identity always included)
  c_voice_allowed   text[] := array['identity','tone','domain','output','safety','tools','channel','refusal','recovery'];
  -- Subagent inherits only these
  c_subagent_only   text[] := array['safety','output','channel'];
begin

  -- ----------------------------------------------------------------
  -- PASO 1: Resolver persona activa para p_firm_id
  -- ----------------------------------------------------------------
  if p_firm_id is not null then
    -- Intentar firma override
    select fpo.*
      into v_fpo
      from firm_personality_overrides fpo
     where fpo.firm_id = p_firm_id
     limit 1;

    if found then
      v_has_fpo := true;
      v_disabled := coalesce(v_fpo.disabled_modules, '{}');
      -- Cargar la persona configurada por la firma
      select ap.*
        into v_persona
        from agent_personas ap
       where ap.id = v_fpo.persona_id
         and ap.is_active = true
       limit 1;
    end if;
  end if;

  -- Si no hay override de firma (o firm_id es null), usar persona system default
  if v_persona.id is null then
    select ap.*
      into v_persona
      from agent_personas ap
     where ap.firm_id is null
       and ap.slug = 'lexai-co-senior-v1'
       and ap.is_active = true
     order by ap.version desc
     limit 1;
  end if;

  -- Guardia: sin persona activa → error descriptivo
  if v_persona.id is null then
    raise exception 'no_active_persona_found: no existe persona activa para firm_id=% y tampoco la persona system default.', p_firm_id;
  end if;

  -- ----------------------------------------------------------------
  -- PASO 2: Cargar módulos habilitados respetando disabled_modules
  -- ----------------------------------------------------------------
  select array_agg(pm order by pm.order_index asc)
    into v_modules
    from personality_modules pm
   where pm.persona_id = v_persona.id
     and pm.enabled = true
     and pm.type <> all(v_disabled);

  if v_modules is null then
    v_modules := '{}';
  end if;

  -- ----------------------------------------------------------------
  -- PASO 3: Cargar D2 (user preferences) anticipado — se inyecta inline
  --         inmediatamente después del módulo S2 (Tone) para que el LLM
  --         no sufra atenuación de contexto distante.
  --         Bug #2 fix (ADR-007): preferencias al inicio, no al final.
  -- ----------------------------------------------------------------
  if p_user_id is not null then
    select tone, brevity, language
      into v_user_tone, v_user_brevity, v_user_language
      from user_personality_preferences
     where user_id = p_user_id
     limit 1;

    if found then
      v_user_pref_line := format(
        'Preferencias del usuario (aplica desde ahora y en CADA respuesta): tratamiento=%s | brevedad=%s | idioma=%s',
        coalesce(v_user_tone, 'usted'),
        coalesce(v_user_brevity, 'normal'),
        coalesce(v_user_language, 'es-CO')
      );
    end if;
  end if;

  -- ----------------------------------------------------------------
  -- PASO 4: Aplicar gating por canal (voice / chat)
  -- PASO 5: Aplicar gating por skill (subagent / skill_doctrine)
  --         Inyecta D2 inline inmediatamente después del módulo type='tone' (S2).
  -- ----------------------------------------------------------------
  foreach v_mod in array v_modules loop
    -- Voice: excluir 'examples' (S10) y tipos no permitidos
    if p_channel = 'voice' and not (v_mod.type = any(c_voice_allowed)) then
      continue;
    end if;

    -- Subagent: solo módulos safety/output/channel
    if p_skill = 'subagent' and not (v_mod.type = any(c_subagent_only)) then
      continue;
    end if;

    -- skill_doctrine: incluir solo si skill_slug coincide con p_skill
    if v_mod.type = 'skill_doctrine' then
      if p_skill is null or v_mod.skill_slug is distinct from p_skill then
        continue;
      end if;
    end if;

    v_parts := array_append(v_parts, v_mod.body_md);

    -- D2 inline injection: justo después del módulo Tone (S2) para compliance máximo.
    -- El bloque lleva header explícito para que el LLM no lo confunda con S2.
    if v_mod.type = 'tone' and v_user_pref_line is not null then
      v_parts := array_append(v_parts,
        '--- PREFERENCIAS DEL USUARIO (ALTA PRIORIDAD — RESPETA EN CADA TURNO) ---' ||
        E'\n' || v_user_pref_line
      );
    end if;
  end loop;

  -- Voice override: añadir instrucción de texto plano al final de los static
  if p_channel = 'voice' then
    v_parts := array_append(v_parts,
      'CANAL VOZ — FORMATO: Responde en texto absolutamente plano. Sin asteriscos, sin guiones de lista, sin Markdown de ningún tipo. Máximo 80 palabras por turno. El motor de audio no renderiza formato.'
    );
  end if;

  -- ----------------------------------------------------------------
  -- PASO 6: Cargar capas DYNAMIC restantes (D1 firm append, D3 session)
  --         D2 ya fue inyectado inline en el bucle de módulos (ver arriba).
  -- ----------------------------------------------------------------

  -- D1: firm append (si existe fpo con system_append_md)
  if v_has_fpo then
    v_firm_append := v_fpo.system_append_md;
  end if;

  -- D3: session override (solo si no ha expirado)
  if p_session_id is not null then
    select append_md
      into v_session_append
      from session_personality_overrides
     where session_id = p_session_id
       and expires_at > now()
     limit 1;
  end if;

  -- ----------------------------------------------------------------
  -- PASO 7: Ensamblar en orden exacto
  -- ----------------------------------------------------------------
  v_prompt := array_to_string(v_parts, E'\n\n');

  -- Bloque dynamic final: D1 firm append + D3 session override
  -- (D2 ya está inyectado inline tras S2)
  if v_firm_append is not null or v_session_append is not null then
    v_prompt := v_prompt || E'\n\n--- instrucciones adicionales del despacho ---';

    if v_firm_append is not null then
      v_prompt := v_prompt || E'\n\n' || v_firm_append;
    end if;

    if v_session_append is not null then
      v_prompt := v_prompt || E'\n\n' || v_session_append;
    end if;
  end if;

  -- ----------------------------------------------------------------
  -- PASO 8: Calcular checksum
  -- Usamos md5() que es nativo de Postgres sin extensiones.
  -- El checksum sirve solo para deduplicación determinista, no para
  -- seguridad criptográfica, por lo que md5 es suficiente.
  -- ----------------------------------------------------------------
  v_checksum := md5(v_prompt);

  -- ----------------------------------------------------------------
  -- PASO 9: INSERT idempotente en agent_personality_versions
  --         firm_id puede ser null (versión system default sin firma).
  -- ----------------------------------------------------------------
  -- INSERT idempotente.
  -- Usamos EXECUTE para evitar la ambigüedad parse-time entre la columna
  -- 'checksum' de agent_personality_versions y el parámetro OUT 'checksum'
  -- de la función RETURNS TABLE.
  execute
    'insert into agent_personality_versions
       (firm_id, persona_id, checksum, system_prompt_snapshot)
     values ($1, $2, $3, $4)
     on conflict (checksum) do nothing
     returning id'
  using p_firm_id, v_persona.id, v_checksum, v_prompt
  into v_version_id;

  -- Si ya existía (on conflict do nothing → no returning), recuperar el id
  if v_version_id is null then
    execute
      'select id from agent_personality_versions where checksum = $1 limit 1'
    using v_checksum
    into v_version_id;
  end if;

  -- ----------------------------------------------------------------
  -- PASO 10: Retornar (asignación a los OUT params de RETURNS TABLE)
  -- Los nombres coinciden con las columnas del RETURNS TABLE.
  -- ----------------------------------------------------------------
  system_prompt  := v_prompt;
  version_id     := v_version_id;
  checksum       := v_checksum;
  return next;

end;
$$;

comment on function lexai_assemble_system_prompt(uuid, uuid, text, text, text) is
  'Sprint P0 · Ensambla el system prompt completo (S1-S10 + D1-D3) con gating de canal y skill. ADR-007 §2.2. Checksum: md5 nativo (sin pgcrypto).';
