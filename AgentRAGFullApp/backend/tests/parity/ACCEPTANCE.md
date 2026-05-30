# Acceptance Gates · S3 paridad A/B legacy vs lean

**Sprint:** M20.04
**Aplica a:** rollout S4 (canary) → S5 (A/B 10%-50%) → S6 (100%)

Antes de avanzar al siguiente sprint, los reportes generados por
`tests/parity/runner_ab.py` + `tests/parity/llm_judge.py` deben cumplir:

## Gates obligatorios

| Gate | Threshold | Cómo medir |
|---|---|---|
| **Calidad LLM-judge** | lean ≥ 9.0/10 (legacy ~9.2) | `llm_judge.py` reporte: `avg score B >= 9.0` |
| **Calidad: % wins lean** | wins(lean) + ties ≥ 60% | `llm_judge.py` `wins.B + wins.tie / total >= 0.6` |
| **Latencia p50** | lean ≤ 50% de legacy | `runner_ab.py` `latency_p50` lean ≤ legacy / 2 |
| **Costo promedio** | lean ≤ 60% de legacy | `runner_ab.py` `cost_avg` lean ≤ legacy * 0.6 |
| **Eventos SSE preservados** | paridad 100% | `tests/contracts/` (S0.3) corre verde contra ambos arms |
| **Errores** | error_rate lean ≤ error_rate legacy | `success_rate` lean ≥ legacy |
| **Bloques generados** | lean ≥ 80% de legacy | `blocks_avg` lean ≥ legacy * 0.8 |
| **Citas grounded** | lean ≥ legacy | `citations_grounded_avg` lean ≥ legacy |

## Gates por complejidad

| Complexity | Latencia max lean (p95) | Min blocks |
|---|---|---|
| `low` | 30s | 4 |
| `medium` | 60s | 6 |
| `high` | 120s | 8 |

## Si NO se cumple un gate

1. **Revisa el reporte MD** (`reports/parity_ab_*.md`) — identifica qué fixture(s) fallaron.
2. **Ajusta el system prompt** del Brain en `lex/brain/system_prompt.py`.
3. **Re-corre** el fixture específico:
   ```bash
   python -m tests.parity.runner_ab --fixture <id> --runs 3 --arm lean
   python -m tests.parity.llm_judge reports/parity_ab_*.json
   ```
4. **Documenta el ajuste** en el commit antes de avanzar.

## Procedimiento estándar pre-S4 canary

```bash
# 1. Captura baseline (una vez)
python scripts/baseline_metrics.py --limit 100

# 2. Corre paridad completa (20 fixtures × 3 runs)
python -m tests.parity.runner_ab --runs 3

# 3. LLM-judge
python -m tests.parity.llm_judge reports/parity_ab_*.json

# 4. Edge cases regresión
pytest tests/parity/test_edge_cases.py -v

# 5. Stress test pool
pytest tests/parity/test_stress_pool.py -v

# 6. Si todo verde → autorizar S4 canary (allowlist 1 firm QA)
```

## Notas

- **Costo de la corrida completa**: ~$5-10 USD (20 fixtures × 3 runs × 2 arms con Sonnet 4.6).
- **Tiempo estimado**: ~45-90 minutos según latencia de Anthropic.
- **Re-correr partes específicas** con `--fixture <id>` o `--limit N` para iteración rápida.
- **No mergear S4 si menos del 80% de fixtures pasan los gates.**
