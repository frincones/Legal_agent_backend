"""
Sprint L-DOC: Admin Pipeline endpoints
=========================================

GET  /admin/pipeline/status        global health + workers + jobs + storage
GET  /admin/pipeline/sources       progreso por fuente (desde ingest_dashboard)
GET  /admin/pipeline/inventory     totales corpus + storage gauges + por fuente
GET  /admin/pipeline/jobs          workers running + cronjobs programados
GET  /admin/pipeline/logs          logs recientes con filtros level/source

Todos requieren autenticacion y admin role (a futuro). Por ahora valida
solo JWT presente. Cuando este listo el admin claim, agregar
require_admin dependency.

Consumido por el frontend /admin/pipeline (Next.js) que proxea estos
endpoints. Si esta tabla aun no se aplico la migracion (auto_migrate
todavia no corrio), devuelve datos vacios + flag schema_missing=true.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from utils.db import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/pipeline", tags=["admin-pipeline"])

SOURCE_DISPLAY_NAMES: dict[str, str] = {
    "corte_cc": "Corte Constitucional",
    "corte_suprema": "Corte Suprema de Justicia",
    "consejo_estado": "Consejo de Estado",
    "suin_juriscol": "SUIN-Juriscol (normas)",
    "senado": "Secretaría del Senado",
    "colombia_compra": "Colombia Compra Eficiente",
    "defensoria": "Defensoría del Pueblo",
    "icbf": "ICBF",
    "minjusticia": "Ministerio de Justicia (SICAAC)",
    "mintrabajo": "Ministerio del Trabajo",
    "ccb": "Cámara de Comercio Bogotá",
    "dian": "DIAN (conceptos)",
    "jep": "JEP",
    "datos_gov_co": "datos.gov.co",
    "hf_datasets": "Hugging Face Datasets",
    "repos_universitarios": "Repositorios universitarios",
    "diario_oficial": "Diario Oficial",
}


async def _require_session(request: Request) -> dict[str, Any]:
    """
    Valida JWT presente (header Authorization: Bearer ...).
    Por ahora NO valida admin role — eso se agrega cuando exista
    el claim correspondiente en JWT. Cualquier usuario autenticado
    puede ver el dashboard (TODO: restringir a admins).
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


async def _table_exists(pool, table_name: str) -> bool:
    """Defensive check: verifica si una tabla existe antes de query."""
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = $1
            )
        """, table_name)


@router.get("/status")
async def get_pipeline_status(
    _claims: dict = Depends(_require_session),
) -> dict[str, Any]:
    """
    Estado global del pipeline: health + workers + jobs + storage + costos.
    """
    storage = await get_storage()
    pool = storage.pool

    # Verificar que tabla ingest_queue exista (auto_migrate corrio)
    if not await _table_exists(pool, "ingest_queue"):
        return _empty_status_payload(schema_missing=True)

    async with pool.acquire() as conn:
        # Counts globales
        counts = await conn.fetchrow("""
            SELECT
                count(*) FILTER (WHERE status = 'pending') AS pending,
                count(*) FILTER (WHERE status = 'processing') AS processing,
                count(*) FILTER (WHERE status = 'failed' AND completed_at > now() - interval '24 hours') AS failed_24h,
                count(*) FILTER (WHERE status = 'completed' AND completed_at::date = current_date) AS done_today,
                count(*) FILTER (WHERE status = 'completed' AND completed_at > now() - interval '24 hours') AS done_24h
            FROM ingest_queue
        """)

        # Storage usage de Postgres (en bytes)
        db_size = await conn.fetchval("SELECT pg_database_size(current_database())")

        # Costo aproximado del dia (desde ingest_runs)
        cost_today_row = await conn.fetchrow("""
            SELECT
                COALESCE(sum(cost_usd) FILTER (WHERE started_at::date = current_date), 0)::numeric AS today,
                COALESCE(sum(cost_usd) FILTER (WHERE started_at > date_trunc('month', now())), 0)::numeric AS month
            FROM ingest_runs
        """)

    pending = int(counts["pending"] or 0)
    processing = int(counts["processing"] or 0)
    failed_24h = int(counts["failed_24h"] or 0)
    done_today = int(counts["done_today"] or 0)
    done_24h = int(counts["done_24h"] or 0)

    # Postgres: free tier 500MB
    pg_mb = (db_size or 0) / 1024 / 1024
    pg_pct = min((pg_mb / 500) * 100, 100)

    # R2: por ahora 0 (no integrado)
    r2_pct = 0.0

    health = "healthy"
    if pg_pct > 90 or failed_24h > 100:
        health = "critical"
    elif pg_pct > 80 or failed_24h > 30:
        health = "degraded"

    return {
        "health": health,
        "active_workers": 0,  # APScheduler no esta corriendo aun
        "total_workers": 0,
        "jobs_queued": pending,
        "jobs_running": processing,
        "jobs_failed_last_24h": failed_24h,
        "docs_ingested_today": done_today,
        "docs_ingested_last_24h": done_24h,
        "storage": {
            "postgres_pct": round(pg_pct, 1),
            "r2_pct": round(r2_pct, 1),
        },
        "cost_today_usd": float(cost_today_row["today"] or 0),
        "cost_month_usd": float(cost_today_row["month"] or 0),
    }


def _empty_status_payload(*, schema_missing: bool = False) -> dict[str, Any]:
    return {
        "health": "healthy",
        "active_workers": 0,
        "total_workers": 0,
        "jobs_queued": 0,
        "jobs_running": 0,
        "jobs_failed_last_24h": 0,
        "docs_ingested_today": 0,
        "docs_ingested_last_24h": 0,
        "storage": {"postgres_pct": 0.0, "r2_pct": 0.0},
        "cost_today_usd": 0.0,
        "cost_month_usd": 0.0,
        "schema_missing": schema_missing,
    }


@router.get("/sources")
async def get_pipeline_sources(
    _claims: dict = Depends(_require_session),
) -> list[dict[str, Any]]:
    """
    Progreso por fuente. Usa vista ingest_dashboard.
    Devuelve TODAS las fuentes conocidas (las que no estan en queue
    aparecen con totals=0 para mostrar como "pendientes").
    """
    storage = await get_storage()
    pool = storage.pool

    if not await _table_exists(pool, "ingest_queue"):
        return _all_sources_empty()

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM ingest_dashboard")

    in_db = {r["source"]: dict(r) for r in rows}
    result = []

    # Para cada fuente conocida, devolver stats reales o vacios
    for source_id, display in SOURCE_DISPLAY_NAMES.items():
        if source_id in in_db:
            r = in_db[source_id]
            total = int(r.get("total") or 0)
            pending = int(r.get("pending") or 0)
            processing = int(r.get("processing") or 0)
            completed = int(r.get("completed") or 0)
            failed = int(r.get("failed") or 0)
            skipped = int(r.get("skipped") or 0)
            result.append({
                "source": source_id,
                "display_name": display,
                "total": total,
                "pending": pending,
                "processing": processing,
                "completed": completed,
                "failed": failed,
                "skipped": skipped,
                "pct_done": float(r.get("pct_done") or 0),
                "avg_seconds_per_doc": float(r["avg_seconds_per_doc"]) if r.get("avg_seconds_per_doc") else None,
                "total_minutes_spent": float(r.get("total_minutes_spent") or 0),
                "last_completed_at": r["last_completed"].isoformat() if r.get("last_completed") else None,
                "eta": r["eta"].isoformat() if r.get("eta") else None,
                "last_error": r.get("last_error"),
            })
        else:
            # Fuente no iniciada aun
            result.append({
                "source": source_id,
                "display_name": display,
                "total": 0,
                "pending": 0,
                "processing": 0,
                "completed": 0,
                "failed": 0,
                "skipped": 0,
                "pct_done": 0,
                "avg_seconds_per_doc": None,
                "total_minutes_spent": 0,
                "last_completed_at": None,
                "eta": None,
                "last_error": None,
            })

    return result


def _all_sources_empty() -> list[dict[str, Any]]:
    return [
        {
            "source": source_id,
            "display_name": display,
            "total": 0,
            "pending": 0,
            "processing": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "pct_done": 0,
            "avg_seconds_per_doc": None,
            "total_minutes_spent": 0,
            "last_completed_at": None,
            "eta": None,
            "last_error": None,
        }
        for source_id, display in SOURCE_DISPLAY_NAMES.items()
    ]


@router.get("/inventory")
async def get_pipeline_inventory(
    _claims: dict = Depends(_require_session),
) -> dict[str, Any]:
    """
    Inventario completo del corpus: totales + storage gauges + por (source, doc_type, materia).
    """
    storage = await get_storage()
    pool = storage.pool

    async with pool.acquire() as conn:
        # Totales globales
        total_docs = await conn.fetchval("SELECT count(*) FROM documents") or 0
        total_chunks = await conn.fetchval("SELECT count(*) FROM chunks") or 0

        # Plantillas
        total_templates = 0
        try:
            total_templates = await conn.fetchval("SELECT count(*) FROM user_templates") or 0
        except Exception:
            pass

        # Sentencias
        total_sentencias = 0
        try:
            total_sentencias = await conn.fetchval("SELECT count(*) FROM jurisprudencia") or 0
        except Exception:
            pass

        # Normas
        total_normas = 0
        try:
            total_normas = await conn.fetchval("SELECT count(*) FROM leyes_normas") or 0
        except Exception:
            pass

        # Storage usage
        db_size = await conn.fetchval("SELECT pg_database_size(current_database())") or 0

        # Costo total (sum de ingest_runs.cost_usd)
        cost_total = 0.0
        try:
            cost_total = float(await conn.fetchval("SELECT COALESCE(sum(cost_usd), 0) FROM ingest_runs") or 0)
        except Exception:
            pass

        # Desglose por (source, doc_type, materia) — usa documents si existe el campo
        by_source: list[dict[str, Any]] = []
        try:
            rows = await conn.fetch("""
                SELECT
                    source,
                    doc_type,
                    NULL::text AS materia,
                    count(*) AS count,
                    max(ingested_at) AS last_ingested_at
                FROM documents
                WHERE source IS NOT NULL
                GROUP BY source, doc_type
                ORDER BY count DESC
                LIMIT 50
            """)
            by_source = [
                {
                    "source": r["source"],
                    "doc_type": r["doc_type"] or "unknown",
                    "materia": r["materia"],
                    "count": int(r["count"]),
                    "last_ingested_at": r["last_ingested_at"].isoformat() if r["last_ingested_at"] else None,
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning("inventory by_source query failed: %s", e)

    pg_mb = db_size / 1024 / 1024

    return {
        "total_documents": int(total_docs),
        "total_chunks": int(total_chunks),
        "total_templates": int(total_templates),
        "total_sentencias": int(total_sentencias),
        "total_normas": int(total_normas),
        "postgres_size_mb": round(pg_mb, 1),
        "postgres_limit_mb": 500,
        "r2_size_gb": 0.0,
        "r2_limit_gb": 10,
        "embeddings_cost_usd": round(cost_total, 2),
        "last_updated_at": "now",
        "by_source": by_source,
    }


@router.get("/jobs")
async def get_pipeline_jobs(
    _claims: dict = Depends(_require_session),
) -> dict[str, Any]:
    """
    Workers ejecutando + cronjobs programados.
    Por ahora APScheduler no esta corriendo, asi que devuelve listas vacias.
    """
    storage = await get_storage()
    pool = storage.pool

    running: list[dict[str, Any]] = []
    cron: list[dict[str, Any]] = []

    if await _table_exists(pool, "ingest_queue"):
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    source,
                    count(*) AS docs_total,
                    count(*) FILTER (WHERE status = 'completed') AS docs_processed,
                    count(*) FILTER (WHERE status = 'processing') AS in_progress,
                    count(*) FILTER (WHERE status = 'failed') AS failed,
                    min(started_at) FILTER (WHERE status = 'processing') AS started_at
                FROM ingest_queue
                WHERE status = 'processing'
                GROUP BY source
            """)
            for r in rows:
                running.append({
                    "job_id": f"job_{r['source']}_active",
                    "source": r["source"],
                    "scheduled_at": (r["started_at"].isoformat() if r["started_at"] else None) or "now",
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "status": "running",
                    "worker_id": None,
                    "current_url": None,
                    "progress_pct": 0.0,
                    "docs_processed": int(r["docs_processed"]),
                    "docs_total": int(r["docs_total"]),
                    "errors_count": int(r["failed"]),
                    "next_retry_at": None,
                })

    return {"running": running, "cron": cron}


@router.post("/run-scraper")
async def run_scraper(
    source: str = Query(..., regex="^(colombia_compra|defensoria|icbf|minjusticia|mintrabajo)$"),
    limit: int = Query(10, ge=1, le=100),
    _claims: dict = Depends(_require_session),
) -> dict[str, Any]:
    """
    Dispara un scraper de plantillas en background.
    Devuelve inmediatamente con job_id; el ingest corre async.
    Por ahora solo colombia_compra esta implementado; el resto devolveran 501.
    """
    if source != "colombia_compra":
        raise HTTPException(status_code=501, detail=f"scraper_{source}_not_implemented")

    import asyncio
    import uuid

    job_id = str(uuid.uuid4())

    async def _run_scraper_bg():
        try:
            from legal_sources.templates.colombia_compra_scraper import ColombiaCompraScraper
        except Exception as e:
            logger.error("Import scraper fallo: %s", e)
            return

        storage = await get_storage()
        pool = storage.pool

        # Crear ingest_run
        run_id = None
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO ingest_runs (source, triggered_by, started_at)
                    VALUES ($1, 'admin_ui', now())
                    RETURNING id
                """, source)
                run_id = str(row["id"])
        except Exception as e:
            logger.warning("ingest_run create fallo: %s", e)

        scraper = ColombiaCompraScraper()
        emitted, skipped, failed = 0, 0, 0
        errors: list[str] = []

        async for cand in scraper.fetch(limit=limit):
            try:
                async with pool.acquire() as conn:
                    existing = await conn.fetchval("""
                        SELECT id FROM template_candidates
                        WHERE source = $1 AND source_ref = $2 LIMIT 1
                    """, cand.source, cand.source_ref)
                    if existing:
                        skipped += 1
                        continue
                    import json as _json
                    await conn.execute("""
                        INSERT INTO template_candidates (
                            id, source, source_ref, source_url,
                            raw_text, normalized_md,
                            suggested_materia, suggested_doc_type, suggested_subtype,
                            suggested_norms, metadata, created_at
                        ) VALUES (
                            gen_random_uuid(), $1, $2, $3,
                            $4, $5,
                            $6, $7, $8,
                            $9, $10::jsonb, now()
                        )
                    """,
                        cand.source, cand.source_ref, cand.source_url,
                        cand.raw_text[:50000], cand.normalized_md,
                        cand.suggested_materia, cand.suggested_doc_type, cand.suggested_subtype,
                        cand.suggested_norms,
                        _json.dumps(cand.metadata or {}),
                    )
                    emitted += 1
            except Exception as e:
                failed += 1
                errors.append(f"{cand.source_ref}: {e}")

        # Update ingest_run
        if run_id:
            try:
                async with pool.acquire() as conn:
                    import json as _json
                    await conn.execute("""
                        UPDATE ingest_runs
                        SET completed_at = now(),
                            docs_processed = $1, docs_failed = $2, docs_skipped = $3,
                            stats_jsonb = $4::jsonb
                        WHERE id = $5
                    """, emitted, failed, skipped,
                        _json.dumps({"errors": errors[:20], "job_id": job_id}),
                        run_id)
            except Exception as e:
                logger.warning("ingest_run update fallo: %s", e)

        logger.info("Scraper %s: emitted=%d skipped=%d failed=%d", source, emitted, skipped, failed)

    # Lanzar background sin esperar
    asyncio.create_task(_run_scraper_bg())

    return {
        "job_id": job_id,
        "source": source,
        "limit": limit,
        "status": "started",
        "message": f"Scraper {source} ejecutandose en background. Verificar /admin/pipeline/inventory en ~30-60 segundos.",
    }


@router.get("/logs")
async def get_pipeline_logs(
    _claims: dict = Depends(_require_session),
    level: str | None = Query(None, regex="^(info|warn|error)$"),
    source: str | None = Query(None, max_length=64),
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """
    Logs recientes con filtros opcionales.
    """
    storage = await get_storage()
    pool = storage.pool

    if not await _table_exists(pool, "pipeline_logs"):
        return []

    where_clauses = ["1=1"]
    params: list[Any] = []
    if level:
        where_clauses.append(f"level = ${len(params) + 1}")
        params.append(level)
    if source:
        where_clauses.append(f"source = ${len(params) + 1}")
        params.append(source)

    params.append(limit)
    sql = f"""
        SELECT ts, level, source, job_id, message, context
        FROM pipeline_logs
        WHERE {' AND '.join(where_clauses)}
        ORDER BY ts DESC
        LIMIT ${len(params)}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    return [
        {
            "ts": r["ts"].isoformat(),
            "level": r["level"],
            "source": r["source"],
            "job_id": r["job_id"],
            "message": r["message"],
            "context": r["context"],
        }
        for r in rows
    ]
