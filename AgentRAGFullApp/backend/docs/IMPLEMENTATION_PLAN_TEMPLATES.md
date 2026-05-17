# Templates · Multi-Agent · Assistant Sidebar · Implementation Plan

> **Status:** Sprint 1 complete · Sprints 2–4 pending.
> **Owners:** backend + frontend.
> **Last updated:** 2026-05-17.

This document is the operator's runbook for the new `templates / multi-agent /
assistant-sidebar` workstream. It explains what shipped in Sprint 1, what to
do to activate it safely, and what each subsequent sprint adds.

## North star

LexAI gets:
1. A **right-side assistant sidebar** that unifies voice + chat in one surface
   (per UX spec U1-U8, V1-V8).
2. A **multi-agent document generator** (LangGraph: PLAN → DRAFT → CRITIC →
   EDIT → VALIDATE) running on top of the existing `firm_skills` system.
3. A **template specialization layer** (system catalog + per-firm overrides
   + materia metadata + quality checklists).
4. A **quality validator** that combines programmatic checklists,
   `vigencia_checker`, and an LLM-as-judge before the document reaches HITL.

Goal: parity with Claude for Legal's quality stack, surpass it for Colombia.

## What's NOT changed

Sprint 1 ships **only new files**. The following systems continue working
unmodified:

- `firm_skills` table + `lexai_resolve_skill` RPC + `lexai_get_active_skills`
- `utils/skill_loader.py` and `utils/skill_runner.py` (single-shot pipeline)
- `firm_playbook` and `playbook_resolver.py`
- `user_templates` (3-level access · adds new columns only)
- All existing `/v1/skills/*` and `/v1/canvas/generate` endpoints
- The voice relay in `api/voice.py` and `_tool_registry`
- The frontend `VoiceProvider`, `VoiceHUD`, `CommandPalette`, `CanvasEditor`

If any of these stops working after applying Sprint 1, **that is a bug**,
not by design.

## Sprint 1 · what shipped

### Backend (new files only)

| File | Purpose |
|---|---|
| `storage/schemas/2026_05_24_templates_specialization.sql` | Additive migration · new columns on `user_templates` + new tables `template_checklists`, `template_candidates`, `template_generations`. **NOT applied automatically.** |
| `utils/llm_tiers.py` | Thin tier-aware wrapper over the existing `utils/llm.py` · resolves `Tier.ROUTER / WORKER / JUDGE` to `gpt-4o-mini / gpt-4o / o3-mini`. |
| `agent/llm_skills/__init__.py` | Public exports for the new primitives. |
| `agent/llm_skills/base.py` | Shared `LLMSkillResult` + `SkillExecutionError`. |
| `agent/llm_skills/router.py` | `classify_intent(...)` · LLM-as-Router. |
| `agent/llm_skills/extractor.py` | `extract_structured(...)` · LLM-as-Extractor. |
| `agent/llm_skills/judge.py` | `judge_quality(...)` · multi-dim rubric scorer. |
| `agent/llm_skills/critic.py` | `critique_section(...)` · per-section reviewer. |

All seven Python modules import cleanly (verified). They are not yet wired
into any production flow — Sprint 3 connects them via the multi-agent
orchestrator.

### Frontend (new files only)

| File | Purpose |
|---|---|
| `lib/assistant/types.ts` | Shared types (Message, PageContext, ActionCard, ActivityItem, etc.). |
| `lib/assistant/context-detector.ts` | Pure `pathname → PageContext`. |
| `lib/stores/assistant-store.ts` | Zustand store + localStorage prefs (`lexai.assistant.prefs.v1`). |
| `components/assistant/AssistantProvider.tsx` | Mount point · hydrates prefs · syncs context. |
| `components/assistant/AssistantSidebar.tsx` | Orchestrator · rail + expanded panel. |
| `components/assistant/AssistantRail.tsx` | Collapsed 56px state. |
| `components/assistant/AssistantHeader.tsx` | Top bar with status + ContextBadge + close. |
| `components/assistant/AssistantThread.tsx` | Unified voice + chat transcript. |
| `components/assistant/AssistantComposer.tsx` | Text + mic stub + slash menu. |
| `components/assistant/ContextBadge.tsx` | "Where is the agent right now" indicator. |
| `components/assistant/index.ts` | Barrel export. |
| `components/assistant/README.md` | Activation guide + architecture. |

`pnpm tsc --noEmit` reported zero errors against the new files (verified).

## How to activate Sprint 1 safely

Each step is reversible. Do them in this order on staging first.

### Step 1 · Apply the migration (backend)

```sql
-- On staging Supabase, in the SQL editor:
\i storage/schemas/2026_05_24_templates_specialization.sql
```

The migration is idempotent. After applying, run the sanity checks at the
bottom of the file. Specifically:

```sql
select column_name, is_nullable, data_type
  from information_schema.columns
 where table_name = 'user_templates'
   and column_name in ('firm_id','materia','subtype','quality_score',
                       'clauses_jsonb','applicable_norms',
                       'ingest_doc_id','usage_count','last_used_at');
-- Expect: firm_id is_nullable = YES, all other columns present.
```

If any existing row has `firm_id IS NOT NULL` (which is all of them today),
nothing changes for current consumers. The new system catalog rows
(`firm_id IS NULL`) are inserted in Sprint 2.

### Step 2 · Enable the frontend feature flag

Add to `.env.local` (frontend):

```
NEXT_PUBLIC_ASSISTANT_SIDEBAR_ENABLED=true
```

This flag has no default — when absent or `'false'`, the sidebar is not
mounted. Existing behavior is preserved.

### Step 3 · Mount in the layout (one-line change)

Inside `app/(app)/layout.tsx`, locate the existing `<VoiceProvider>` block.
Add **one** line near the other globals (`<CommandPalette />`,
`<HITLController />`):

```tsx
import { AssistantProvider } from '@/components/assistant';

const assistantEnabled = process.env.NEXT_PUBLIC_ASSISTANT_SIDEBAR_ENABLED === 'true';

// ...inside the JSX, next to <CommandPalette />:
{assistantEnabled && <AssistantProvider />}
```

That is the entire integration. Other components remain untouched.

### Step 4 · Manual smoke test

With the flag on:
1. `/casos/[any-id]` → rail visible on the right edge with 4 icons.
2. Click chat icon → expanded panel opens, ContextBadge shows the matter.
3. Type a message + Enter → local echo response in ~600ms (Sprint 2 wires
   the real backend call).
4. Click voice mic icon → header dot turns green (Sprint 3 wires actual
   voice activation).
5. Close + reload → expansion state persisted per browser.
6. `/casos` (list) → ContextBadge shows "🏛️ Mis casos".
7. `/settings/*` → ContextBadge shows "⚙️ Configuración".
8. Toggle flag off → sidebar disappears completely, no UI artifact.

If any of those fails, do not proceed to Sprint 2.

## Sprint 2 · scope

Goal: templates ingestion + RAG search.

Backend:
- `legal_sources/templates/secop_scraper.py` (SECOP datos.gov.co API)
- `legal_sources/templates/rama_judicial_formatos.py`
- `legal_sources/templates/min_trabajo_modelos.py`
- `legal_sources/templates/style_guide_seeder.py` (1 base + 11 overlays)
- `scripts/seed_system_templates.py` (10 curated rows)
- `api/templates_search.py` exposing `GET /v1/templates/search?q=...`

Frontend:
- `components/templates/TemplateSearchPicker.tsx` (semantic search UI)
- Add "Catálogo Colombia" tab to existing `TemplatesManager.tsx`
- `app/api/templates/search/route.ts` (proxy to backend)
- Wire `AssistantComposer.onSend` to `POST /v1/skills/execute/stream`

Acceptance: typing "demanda laboral despido" in the assistant returns 5
template suggestions ranked by quality_score + semantic similarity.

## Sprint 3 · scope

Goal: multi-agent generator + voice unified in sidebar + responsive +
agentic UI primitives.

Backend:
- `agent/workers/document_generator.py` (LangGraph StateGraph:
  PLAN → DRAFT (parallel by section) → CRITIC → EDIT → VALIDATE)
- `agent/tools/slot_filler.py` (uses `document_extractions` + extractor)
- `agent/tools/quality_validator.py` (checklist + vigencia + judge)
- Extend `utils/skill_runner.py` with optional `mode='multi_agent'`
  (backward compatible · default is current single-shot)
- Adapt `agent/tools/draft_pleading.py` to delegate to the new generator
  while preserving its tool signature

Frontend:
- `components/assistant/VoiceOrb.tsx` (reactive orb)
- Mount `VoiceProvider` / `RealtimeClient` inside the sidebar header
- `ActionCard` · `TaskCard` · `ReadyToSendCard`
- `ActivityTimeline.tsx` (reads `skill_executions` + `agent_traces`)
- Tabs in `GenerateWritDialog`: "Con plantilla" (default) / "Libre"
- Resize support (U8 · drag border · persist per user)
- Responsive: push ≥1280, overlay 768-1280, bottom-sheet <768 with
  floating orb persistent (U3)

Acceptance: a user can dictate "redacta una demanda laboral por despido
de Juan contra Bavaria" and see the document stream into the canvas while
the multi-agent runs PLAN/DRAFT/CRITIC/EDIT/VALIDATE in background.

## Sprint 4 · scope

Goal: quality at scale + curation + handoff + onboarding.

Backend:
- Seed 28 `template_checklists` (matrix materia × kind for CO priorities)
- `eval/skill_qa.py` (Anthropic-style 9 Design Parameters as a CI suite)
- `eval/templates_eval.py` (50 prompts curated by a lawyer)
- `agent/workers/template_ingestion_scheduler.py` (weekly cron)
- `agent/workers/playbook_monitor.py` (detect deviation patterns)
- `case_state` as shared voz↔chat state (V5+V6)

Frontend:
- `app/(app)/admin/templates/review/page.tsx` (curator workflow)
- `QualityChecklistBadge` integrated into `CitationsSidebar`
- `ProactiveNudge` with anti-friction (silenced after 3 dismiss/matter)
- Cross-matter scope tools in `/casos`
- `app/(app)/settings/firm/profile/page.tsx` editing `firm_playbook.raw_md`
- E2E Playwright suite: voz → canvas → HITL → approval (flag on AND off)
- Metrics instrumentation: `sidebar_opened`, `voice_activated`,
  `card_accepted`, `faithfulness_score`, `hallucination_rate`
- Onboarding tooltip (U5)

Acceptance: lawyer NPS ≥ 4.3/5 in internal pilot · checklist pass rate
≥ 85% · hallucination rate < 5%.

## Glossary

| Term | Meaning |
|---|---|
| Materia | Practice area · enum `materia_legal` in DB (11 values). |
| Kind / doc_kind | Document type · enum `doc_kind` in DB (`demanda`, `contestacion`, etc.). |
| Subtype | Finer classification, e.g. `tutela_salud_eps`. |
| System catalog | Templates with `firm_id IS NULL`, visible to all firms. |
| Playbook | `firm_playbook.raw_md` · per-firm CLAUDE.md equivalent. |
| Skill | A row in `firm_skills` invokable via `/v1/skills/execute`. |
| Primitive | A reusable LLM call in `agent/llm_skills/` (router/extractor/judge/critic). |
| Multi-agent | LangGraph orchestrator that chains primitives (Sprint 3). |

## Contact

Questions / blockers: ping the LexAI engineering channel.
