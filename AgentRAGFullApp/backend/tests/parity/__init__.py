"""Sprint M20.04 · S3 · paridad A/B legacy vs lean.

Suite de validación que corre cada fixture × N veces contra ambos arms
y compara métricas + calidad (LLM-judge). Genera reporte JSON + Markdown.

Uso:
    cd backend
    python -m tests.parity.runner_ab --limit 5 --runs 1 --arm both

Outputs:
    reports/parity_ab_YYYY_MM_DD_HHMMSS.json
    reports/parity_ab_YYYY_MM_DD_HHMMSS.md
"""
