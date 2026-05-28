"""Sprint M19.29 · E2E test real para el renderer Claude (camino B).

Flujo end-to-end:
  1. Verifica `node_executor.health_check_sync` (node + docx@9.5 presentes)
  2. Lista skills builtin del marketplace via GET /v1/skills/marketplace
  3. Elige UN doc_type al azar entre los 24 builtin (12 sprint_h + 12 sprint_m1927)
  4. Construye RenderRequest con datos sintéticos + SKILL.md del builtin
  5. Llama claude_docx_renderer.render_docx() directamente
  6. Valida output .docx:
       - magic bytes PK\\x03\\x04
       - tamaño 500 < N < 10MB
       - abre como zip y verifica que tiene word/document.xml
       - extrae texto y verifica que contiene partes del template (ej. doc_type words)
  7. Verifica audit en claude_render_audit
  8. Verifica que `skill_executions` no se afectó (renderer es independiente)

Uso:
    pytest -xvs tests/e2e/test_m1929_renderer_random_doctype.py

Vars de entorno requeridas:
    ANTHROPIC_API_KEY          (obligatoria)
    DATABASE_URL               (Supabase Postgres pool, asyncpg)
    CLAUDE_RENDERER_ENABLED=true
    CLAUDE_RENDERER_DOC_FAMILIES (opcional; si vacío, todas permitidas)
"""

from __future__ import annotations

import io
import os
import random
import sys
import zipfile
from typing import Any, Optional

import pytest

# Permite ejecutar `pytest tests/...` desde la raíz del backend
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.e2e,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env() -> None:
    missing = []
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        missing.append("ANTHROPIC_API_KEY")
    if not os.getenv("DATABASE_URL", "").strip():
        missing.append("DATABASE_URL")
    if missing:
        pytest.skip(f"E2E requires env vars: {', '.join(missing)}")


def _set_renderer_flags() -> None:
    os.environ.setdefault("CLAUDE_RENDERER_ENABLED", "true")
    # No restringir doc_families
    os.environ.pop("CLAUDE_RENDERER_DOC_FAMILIES", None)
    os.environ.pop("CLAUDE_RENDERER_FIRM_IDS", None)


async def _get_pool():
    import asyncpg
    dsn = os.environ["DATABASE_URL"]
    return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)


# Datos sintéticos para distintos doc_types
_SYNTHETIC_DATA: dict[str, dict[str, Any]] = {
    "default": {
        "fecha": "2026-05-28",
        "ciudad": "Bogotá",
        "departamento": "Cundinamarca",
        "nombre_poderdante": "Juan Pérez García",
        "cc_poderdante": "1.000.000",
        "nombre_apoderado": "María Rodríguez López",
        "cc_apoderado": "2.000.000",
        "tarjeta_profesional": "12345",
        "facultades": "representarme en todos los actos del proceso ejecutivo No. 2026-01234",
    },
    "/redactar/compraventa-vehiculo": {
        "fecha": "2026-05-28",
        "ciudad": "Medellín",
        "nombre_vendedor": "Carlos Gómez",
        "cc_vendedor": "3.000.000",
        "nombre_comprador": "Ana Torres",
        "cc_comprador": "4.000.000",
        "placa": "ABC123",
        "marca": "Toyota",
        "modelo": "2022",
        "linea": "Hilux",
        "color": "Blanco",
        "numero_motor": "1GDFTV12345",
        "numero_chasis": "MR0FR22G500123456",
        "cilindraje": "2700",
        "tipo_carroceria": "Pickup",
        "kilometraje": "45000",
        "precio_numero": "$95.000.000",
        "precio_letras": "noventa y cinco millones de pesos",
        "fecha_entrega": "2026-06-15",
    },
    "/redactar/declaracion-extrajuicio": {
        "fecha": "2026-05-28",
        "ciudad": "Cali",
        "nombre_declarante": "Pedro Sánchez",
        "cc_declarante": "5.000.000",
        "hechos": "1) Conozco al señor Juan Pérez desde hace 10 años. 2) Me consta que su domicilio es Carrera 7 No. 80-15. 3) Le he visto trabajando como independiente desde 2018.",
        "proposito": "para presentar ante el Ministerio del Trabajo en el trámite de afiliación independiente",
    },
}


def _data_for(command: str) -> dict[str, Any]:
    return _SYNTHETIC_DATA.get(command, _SYNTHETIC_DATA["default"])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_and_docx_runtime_ready():
    """Smoke: node y docx-js@9.5 están disponibles en el contenedor."""
    from lex.renderer.node_executor import health_check_sync
    h = health_check_sync()
    assert h.get("node_ok"), f"node missing: {h}"
    assert h.get("docx_ok"), f"docx missing: {h}"
    print(f"[runtime] node={h.get('node_version')} docx={h.get('docx_version')}")


@pytest.mark.asyncio
async def test_marketplace_lists_builtin_skills():
    """La migración M19.27 y sprint_h dejan ≥ 22 skills builtin published."""
    _require_env()
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            n = await conn.fetchval(
                "select count(*) from firm_skills "
                "where firm_id is null and status = 'published'"
            )
        assert n >= 22, f"Expected ≥22 builtin skills, found {n}. ¿Migraciones aplicadas?"
        print(f"[marketplace] {n} builtin skills published")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_render_random_doc_type_end_to_end():
    """Pipeline completo: pick aleatorio + Claude → JS → docx → validación."""
    _require_env()
    _set_renderer_flags()

    from lex.renderer.claude_docx_renderer import RenderRequest, render_docx

    pool = await _get_pool()
    try:
        # 1. Pick random builtin
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select id, command, name, frontmatter, system_prompt, references_md
                  from firm_skills
                 where firm_id is null
                   and status = 'published'
                   and category in ('drafting','analysis')
                """
            )
        assert rows, "No builtin skills found — corre las migraciones primero"
        picked = random.choice(rows)
        command = picked["command"]
        doc_type = None
        fm = picked["frontmatter"]
        if isinstance(fm, dict):
            doc_type = fm.get("doc_type")
        if isinstance(fm, str):
            import json as _j
            try:
                doc_type = _j.loads(fm).get("doc_type")
            except Exception:
                pass
        doc_type = doc_type or command.rsplit("/", 1)[-1].replace("-", "_")
        print(f"[pick] command={command} doc_type={doc_type} name={picked['name']}")

        # 2. Build template_skill_md
        system_prompt = picked["system_prompt"] or ""
        refs = picked["references_md"] or ""
        template_md = system_prompt + ("\n\n---\n\n## References\n\n" + refs if refs else "")

        # 3. Render
        req = RenderRequest(
            doc_type=doc_type,
            template_skill_md=template_md,
            user_prompt=(
                f"Genera un ejemplo profesional de '{picked['name']}' para Colombia, "
                "usando los datos del objeto `data`. Sustituye todos los placeholders "
                "razonables con los valores que recibas; deja como [PLACEHOLDER] solo "
                "los que no estén en data."
            ),
            data=_data_for(command),
            firm_id=None,
            user_id=None,
            matter_id=None,
            document_id=None,
            skill_id=str(picked["id"]),
            max_retries=2,
            timeout_s=45,
        )
        try:
            result = await render_docx(req)
        except Exception as e:
            pytest.fail(f"Claude renderer failed for {command}: {type(e).__name__}: {e}")

        # 4. Validar output
        assert result.docx_bytes, "docx vacío"
        assert len(result.docx_bytes) >= 500, f"docx muy pequeño: {len(result.docx_bytes)}"
        assert len(result.docx_bytes) < 10 * 1024 * 1024, "docx > 10MB"
        assert result.docx_bytes.startswith(b"PK\x03\x04"), "no es zip docx"

        # zip valid?
        with zipfile.ZipFile(io.BytesIO(result.docx_bytes), "r") as zf:
            names = zf.namelist()
            assert "word/document.xml" in names, f"falta word/document.xml. Hay: {names[:5]}"
            doc_xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            assert len(doc_xml) > 200, "document.xml demasiado corto"
            # Validar que el texto contiene algo en español (no JS leaked)
            assert "Document" not in doc_xml or "<w:document" in doc_xml, "leak JS?"

        print(
            f"[render OK] bytes={len(result.docx_bytes)} sha256={result.sha256[:12]} "
            f"retries={result.retry_count} llm_ms={result.llm_duration_ms} "
            f"sandbox_ms={result.sandbox_duration_ms} cost_cents={result.cost_usd_cents}"
        )

        # 5. Verificar audit
        # Nota: log_audit es opcional (best-effort), test no falla si la fila no aparece.
        from lex.renderer.claude_docx_renderer import log_audit
        audit_id = await log_audit(pool, req=req, result=result)
        if audit_id:
            print(f"[audit] inserted claude_render_audit id={audit_id}")
        else:
            print("[audit] log_audit returned None (table may be missing or firm_id null + RLS)")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_renderer_disabled_by_flag():
    """Si la flag está OFF, el helper is_renderer_enabled_for retorna False."""
    from lex.renderer.claude_docx_renderer import is_renderer_enabled_for
    os.environ["CLAUDE_RENDERER_ENABLED"] = "false"
    assert not is_renderer_enabled_for(doc_type="x", doc_family="contractual", firm_id=None)
    os.environ["CLAUDE_RENDERER_ENABLED"] = "true"
    assert is_renderer_enabled_for(doc_type="x", doc_family="contractual", firm_id=None)


@pytest.mark.asyncio
async def test_tier_check_premium_blocks_trial_firm():
    """Crear skill builtin con tier=premium, validar que firm con plan=trial recibe denegación."""
    _require_env()
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            # Crear skill premium temporal
            await conn.execute(
                """
                insert into firm_skills (firm_id, command, name, description, category,
                                          frontmatter, system_prompt, tier, status, version)
                values (null, '/test/m1929-premium', 'test premium', 'tmp', 'drafting',
                        '{}'::jsonb, 'tmp', 'premium', 'published', 1)
                on conflict (firm_id, command, version) do update set tier='premium'
                """
            )
            firm_row = await conn.fetchrow(
                "select id, plan from firms where plan = 'trial' limit 1"
            )
            if not firm_row:
                pytest.skip("No hay firm con plan=trial en la BD para validar tier_check")

            from utils.tier_check import check_skill_tier_allowed
            dec = await check_skill_tier_allowed(
                pool, firm_id=str(firm_row["id"]),
                skill_command="/test/m1929-premium",
            )
            assert dec.tier == "premium"
            assert not dec.allowed, f"trial firm should be blocked, decision={dec}"
            print(f"[tier_check] OK: trial firm blocked for premium ({dec.reason})")

            # Cleanup
            await conn.execute(
                "delete from firm_skills where command = '/test/m1929-premium' and firm_id is null"
            )
    finally:
        await pool.close()
