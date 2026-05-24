# Citation Verification System — LexAI

**Status**: M15 deployado · `USE_VERIFICATION_AGENT` flag controlado por env var
**Target**: Tolerancia 0% a alucinaciones de citas legales

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────────────┐
│                      ORCHESTRATOR (Stage 6)                         │
│                                                                     │
│   if USE_VERIFICATION_AGENT=1:                                      │
│      → VerificationAgent.verify_batch(citations)                    │
│   else:                                                             │
│      → CitationVerifier (legacy M9)                                 │
│                                                                     │
│   if SHADOW_MODE=1: ambos corren, legacy gana, diffs logged         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   VerificationAgent.verify(citation)                │
│                                                                     │
│   1. NORMALIZATION                                                  │
│      ├─ parse_citation_ref() (con expand_to_canonical de M13)       │
│      └─ llm_normalize_citation() (fallback gpt-4o-mini M15)         │
│                                                                     │
│   2. CACHE GATE (external_fetch_cache, TTL por kind)                │
│                                                                     │
│   3. TOOL DISPATCHER (determinístico por parsed.kind)               │
│      ├─ jurisprudencia CC  → SearchInternalDB + FetchCorteCC        │
│      ├─ jurisprudencia CSJ → SearchInternalDB + FetchCSJRss         │
│      ├─ ley/decreto        → SearchInternalDB + FetchSenadoSuin     │
│      │                       + CheckDerogation                      │
│      ├─ codigo_articulo    → LookupArticuloChunks + SearchInternalDB│
│      └─ codigo             → SearchInternalDB + CheckDerogation     │
│                                                                     │
│   4. EVIDENCE ACCUMULATOR                                           │
│      ├─ Combina resultados (max wins, +0.02 boost por corroborador) │
│      └─ Reglas críticas: NUNCA verificada solo por embedding        │
│                                                                     │
│   5. VERDICT (4 estados)                                            │
│      ├─ confidence ≥ 0.90 → verificada (✅)                         │
│      ├─ 0.70 - 0.89       → sospechosa  (⚠️)                        │
│      ├─ derogada=true     → superada    (⚠️)                        │
│      └─ < 0.70            → no_encontrada (❌)                      │
│                                                                     │
│   6. PERSIST                                                        │
│      ├─ external_fetch_cache (TTL 7-30d)                            │
│      └─ verification_attempts (audit trail)                         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Tools

| Tool | Wraps | Latency | Cost |
|---|---|---|---|
| SearchInternalDB | utils/citation_verifier legacy | <50ms (cache) | $0 |
| FetchCorteCC | legal_sources/corte_constitucional.py | 400-1200ms | $0 |
| FetchCSJRss | legal_sources/csj_source.py (M11) | 300-800ms | $0 |
| FetchSenadoSuin | legal_sources/senado_scraper.py | 300-1000ms | $0 |
| LookupArticuloChunks | ILIKE en chunks corpus | <100ms | $0 |
| CheckDerogation | lex/verify/derogation_verifier | <50ms | $0 |
| LLMNormalizer (fallback) | gpt-4o-mini function calling | 300-600ms | $0.0003 |

---

## Feature flags

```bash
# Legacy path (M9), default
USE_VERIFICATION_AGENT=0

# Nuevo agent activo, legacy retirado
USE_VERIFICATION_AGENT=1
SHADOW_MODE=0

# Shadow mode: ambos corren, legacy gana, diffs en BD
USE_VERIFICATION_AGENT=1
SHADOW_MODE=1
```

### Rollout recomendado

1. **Fase 1**: deploy M14 con `USE_VERIFICATION_AGENT=0` (cero riesgo)
2. **Fase 2**: activar `USE_VERIFICATION_AGENT=1` + `SHADOW_MODE=1` por 48h
3. **Fase 3**: revisar `v_verification_shadow_summary` — si `critical=0`, proceder
4. **Fase 4**: setear `SHADOW_MODE=0` (agent es source-of-truth)

---

## Endpoints admin

```
GET /v1/admin/citations/health
  → métricas 7d agrupadas por estado/source

GET /v1/admin/citations/recent-failures?limit=20
  → últimas N citas no_encontrada/sospechosa/error

GET /v1/admin/citations/shadow-diffs?critical_only=true
  → divergencias del SHADOW_MODE (M14)
```

UI: `https://lexai-frontend-rho.vercel.app/admin/citations`

---

## Gold set + eval

```bash
# Local (sólo parser)
python scripts/eval_citation_gold_set.py

# Contra producción
python scripts/eval_citation_gold_set.py --prod
```

**Targets**:
- Citation Existence Rate (CER) ≥ 95%
- False Positive Rate (FPR) ≤ 5%
- False Negative Rate (FNR) ≤ 5%
- Latencia p95 ≤ 4s
- Costo ≤ $0.005 por documento

Gold set: 70 citas en `tests/fixtures/gold_set.py`:
- 20 sentencias Corte Constitucional
- 15 sentencias CSJ
- 15 leyes reales
- 5 decretos reales
- 10 código+artículo (formato libre LLM)
- 5 alucinaciones intencionales para medir FPR

---

## Schema SQL

```sql
-- Persistencia de cada verificación
verification_attempts:
  citation_ref text
  ref_type text
  result_state ('verificada'|'sospechosa'|'no_encontrada'|'superada'|'error')
  source text  -- 'cache' | 'legacy:bd' | 'csj_rss_exact' | etc.
  confidence_score float
  sources_tried jsonb
  normalized_ref text
  tool_results jsonb
  duration_ms int

-- Cache de resultados (TTL por kind)
external_fetch_cache:
  cache_key text  -- 'v1:juris:T:760:2008'
  content_jsonb jsonb
  ttl_seconds int

-- Shadow mode diffs (M14)
verification_shadow_diffs:
  citation_ref, citation_type
  legacy_state, legacy_method, legacy_fuente_url
  agent_state, agent_method, agent_confidence, agent_fuente_url
  diff_type ('identical'|'minor'|'medium'|'critical')
  is_critical boolean
```

---

## Decisiones arquitectónicas clave

### ¿Por qué NO usar LLM como verificador?

Harvey AI/Casetext usan LLM-as-verifier porque tienen Westlaw API estructurada
(millones de docs con IDs únicos ambiguos). LexAI tiene fuentes determinísticas:
URL predecible (Corte CC), API estructurada (SUIN), RSS feed (CSJ). **No hay
ambigüedad semántica que un LLM deba resolver.**

| Aspecto | LLM verifier | Tool dispatcher |
|---|---|---|
| Costo por cita | $0.005-0.015 | $0 (excepto fallback) |
| Latencia | 2-4s | 0.5-1.5s |
| Determinismo | bajo | 100% |
| Ideal para | APIs ambiguas | URLs predecibles |

### ¿Por qué SHADOW_MODE?

Reduce riesgo de regresión a casi cero: el nuevo agent corre en paralelo al
legacy en producción real, pero el legacy gana (usuario ve resultado legacy).
Los diffs se loggean en BD para validar 48h antes del flip definitivo.

### ¿Por qué NUNCA verificada solo por embedding?

El embedding puede encontrar un artículo *relacionado* (sim 0.85), no la norma
exacta citada. Para anti-alucinación absoluta, requerimos al menos 1 fuente
autoritativa (BD cache, live fetch URL, RSS exact match).
