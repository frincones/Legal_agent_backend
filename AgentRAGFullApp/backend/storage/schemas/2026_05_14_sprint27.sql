-- ============================================================
-- LexAI · Sprint 27 · Landing + Pricing público + Changelog + Testimonials
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. changelog_entries · entradas públicas del changelog
-- ------------------------------------------------------------
create table if not exists changelog_entries (
  id             uuid primary key default gen_random_uuid(),
  slug           text unique not null,
  title          text not null,
  summary        text,
  body_md        text not null,
  category       text not null default 'feature'
    check (category in ('feature','improvement','fix','breaking','announcement')),
  version        text,
  released_at    timestamptz default now(),
  highlighted    boolean default false,
  published      boolean default true,
  metadata       jsonb default '{}'::jsonb,
  created_by     uuid references admin_users(id),
  created_at     timestamptz default now(),
  updated_at     timestamptz default now()
);
create index if not exists changelog_published_idx
  on changelog_entries (released_at desc) where published = true;
create index if not exists changelog_highlighted_idx
  on changelog_entries (released_at desc) where highlighted = true;

alter table changelog_entries enable row level security;
drop policy if exists changelog_select on changelog_entries;
drop policy if exists changelog_modify on changelog_entries;
create policy changelog_select on changelog_entries for select using (published = true);
create policy changelog_modify on changelog_entries for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 2. testimonials · social proof para landing/customers
-- ------------------------------------------------------------
create table if not exists testimonials (
  id             uuid primary key default gen_random_uuid(),
  slug           text unique not null,
  author_name    text not null,
  author_role    text,
  firm_name      text,
  firm_logo_url  text,
  avatar_url     text,
  quote          text not null,
  rating         int check (rating between 1 and 5),
  use_case       text,
  area_practica  text,
  country        text default 'CO',
  featured       boolean default false,
  published      boolean default true,
  sort_order     int default 100,
  created_at     timestamptz default now(),
  updated_at     timestamptz default now()
);
create index if not exists testimonials_featured_idx
  on testimonials (sort_order asc, created_at desc)
  where published = true and featured = true;
create index if not exists testimonials_published_idx
  on testimonials (sort_order asc, created_at desc)
  where published = true;

alter table testimonials enable row level security;
drop policy if exists testimonials_select on testimonials;
drop policy if exists testimonials_modify on testimonials;
create policy testimonials_select on testimonials for select using (published = true);
create policy testimonials_modify on testimonials for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 3. landing_stats RPC · números agregados para social proof
-- ------------------------------------------------------------
create or replace function lexai_landing_stats()
returns jsonb
language sql
stable
as $$
  select jsonb_build_object(
    'firms_total',       (select count(*) from firms),
    'firms_active',      (select count(*) from firm_subscriptions where status in ('active','trialing')),
    'matters_total',     (select count(*) from matters),
    'documents_total',   (select count(*) from matter_documents),
    'citations_total',   (select count(*) from document_citations),
    'snapshot_at',       now()
  );
$$;
grant execute on function lexai_landing_stats() to authenticated, service_role, anon;

-- ============================================================
-- SEEDS · changelog inicial
-- ============================================================
insert into changelog_entries (slug, title, summary, body_md, category, version, released_at, highlighted, published) values
  ('s25-entitlements', 'Sistema de entitlements granular',
   'Modulos individuales por plan + cuotas custom por firm.',
   '# Entitlements granular

Ahora cada plan tiene asignación granular de los 69 módulos del producto. Owner puede ajustar:
- Qué módulos incluye cada plan (matriz visual)
- Cuotas por kind (LLM calls, voz, docs, etc.)
- Overrides per-firm con expiración para casos custom

Disponible desde el panel /saas/modules y /saas/quotas.',
   'feature', 'v2.5.0', now() - interval '1 day', true, true),

  ('s26-onboarding', 'Onboarding cero-fricción + Lex Helper',
   'Asistente embebido, checklist de activación y datos demo al signup.',
   '# Onboarding mejorado

Al registrarte ahora obtienes:
- 2 clientes y 2 casos de ejemplo (puedes borrarlos en 1 click)
- Activation Checklist persistente con 5 pasos
- **Lex Helper**: botón flotante "?" con tips contextuales por pantalla
- 4 emails de bienvenida automáticos (D0, D1, D3, D7)',
   'feature', 'v2.6.0', now(), true, true),

  ('s24-saas-admin', 'Panel SaaS Admin completo',
   'Gestión multi-tenant: firms, usuarios, cartera, feature flags, soporte.',
   'Panel /saas/* con:
- Dashboard MRR/ARR/churn
- Gestión cross-firm de tenants y usuarios
- Cartera con aging buckets
- Feature flags + per-firm overrides
- Tickets de soporte con threading + notas internas
- Impersonate read-only auditado',
   'feature', 'v2.4.0', now() - interval '2 days', false, true),

  ('s23-paddle-billing', 'Paddle Billing + Free plan auto-onboard',
   'Trial 14 días sin tarjeta + cuotas en tiempo real.',
   'Sistema de billing completo:
- Auto-asigna plan Free al signup (trial 14d)
- Cuotas en tiempo real con QuotaBanner contextual
- UpgradeModal automático en 402
- Mock provider para demo · Paddle real con env vars',
   'feature', 'v2.3.0', now() - interval '3 days', false, true),

  ('s22-wizards-public', 'Trámites ciudadanos (B2C)',
   'Wizards públicos sin login para tutelas, derechos de petición, pensiones.',
   'Acceso público en /tramites para que ciudadanos puedan generar:
- Derecho de petición
- Tutela básica
- Solicitud de pensión

Sin registro · genera documento DOCX descargable + email a Defensoría opcional.',
   'feature', 'v2.2.0', now() - interval '5 days', false, true),

  ('s20-judges', 'Simulador de jueces con IA',
   'Predice cómo recibirá tu escrito un juez específico.',
   'Base de 15 magistrados colombianos (Corte Constitucional, Suprema, Consejo de Estado, Tribunales).
- Búsqueda + filtros por corte
- Perfil con decisiones recientes
- Simulación IA: cómo recibirá tu escrito (alignment score + fortalezas + riesgos)
- Cache 24h por documento_hash',
   'feature', 'v2.0.0', now() - interval '14 days', false, true)
on conflict (slug) do nothing;

-- ============================================================
-- SEEDS · testimonios iniciales (ejemplos · admin puede editar)
-- ============================================================
insert into testimonials (slug, author_name, author_role, firm_name, quote, rating, area_practica, country, featured, sort_order, published) values
  ('nielssen-honduras', 'Nielssen Carvajal', 'Abogada · Contencioso administrativo',
   'Instituto de Pensiones', 'La fundamentación jurídica con IA verificada es lo que más valor agrega. Ya no me preocupa citar normas derogadas.',
   5, 'administrativo', 'HN', true, 10, true),

  ('natalia-armenia', 'Natalia Vargas', 'Abogada independiente', null,
   'Hago liquidaciones laborales en 30 segundos. Lo que antes me tomaba media tarde.',
   5, 'laboral', 'CO', true, 20, true),

  ('socio-bogota', 'Carlos Méndez', 'Socio Senior', 'Méndez & Asociados',
   'Court Watcher nos eliminó las visitas semanales al juzgado. Cada cambio de audiencia llega antes que a la contraparte.',
   5, 'civil', 'CO', true, 30, true)
on conflict (slug) do nothing;

-- ============================================================
-- Done · Sprint 27 migration
-- ============================================================
