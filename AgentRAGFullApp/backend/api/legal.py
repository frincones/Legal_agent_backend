"""
API endpoints para búsqueda legal, verificación de vigencia e ingesta de normas.
"""
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/legal", tags=["legal"])

# Lazy-initialized references (set by main.py on startup)
_source_router = None
_derogation_graph = None
_vigencia_checker = None
_storage = None
_embedder = None


def init_legal_api(source_router, derogation_graph, vigencia_checker, storage, embedder):
    """Called by main.py on startup to inject dependencies."""
    global _source_router, _derogation_graph, _vigencia_checker, _storage, _embedder
    _source_router = source_router
    _derogation_graph = derogation_graph
    _vigencia_checker = vigencia_checker
    _storage = storage
    _embedder = embedder


# --- Request/Response Models ---

class LegalSearchRequest(BaseModel):
    query: str
    sources: Optional[list[str]] = None  # None = all enabled
    limit: int = 10
    tipo: Optional[str] = None
    anio: Optional[int] = None


class VigenciaRequest(BaseModel):
    tipo: str
    numero: int
    anio: int


class IngestNormaRequest(BaseModel):
    source: str = "senado"  # senado, funcion_publica
    tipo: str
    numero: int
    anio: int


class JurisprudenciaSearchRequest(BaseModel):
    query: str
    corte: Optional[str] = None
    tipo_sentencia: Optional[str] = None
    desde: Optional[str] = None
    limit: int = 10


# --- Endpoints ---

@router.post("/search")
async def search_legal_sources(req: LegalSearchRequest):
    """Busca en fuentes legales vivas (internet)."""
    if not _source_router:
        raise HTTPException(503, "Legal sources not initialized")

    result = await _source_router.search(
        query=req.query,
        limit=req.limit,
        sources=req.sources,
        tipo=req.tipo,
        anio=req.anio,
    )

    # Serializar LiveSourceResult objects
    serialized_results = []
    for r in result["results"]:
        serialized_results.append({
            "source": r.source,
            "tipo": r.tipo,
            "numero": r.numero,
            "anio": r.anio,
            "titulo": r.titulo,
            "estado": r.estado,
            "url": r.url,
            "preview": r.preview,
            "metadata": r.metadata,
        })

    return {
        "results": serialized_results,
        "sources_consulted": result["sources_consulted"],
        "duration_ms": result["duration_ms"],
        "errors": result["errors"],
    }


@router.post("/vigencia")
async def check_vigencia(req: VigenciaRequest):
    """Verifica la vigencia de una norma."""
    if not _vigencia_checker:
        raise HTTPException(503, "Vigencia checker not initialized")

    result = await _vigencia_checker.check(
        tipo=req.tipo,
        numero=req.numero,
        anio=req.anio,
        check_live_sources=True,
    )

    return {
        "norma_id": result.norma_id,
        "tipo": result.tipo,
        "numero": result.numero,
        "anio": result.anio,
        "titulo": result.titulo,
        "estado": result.estado,
        "encontrada": result.encontrada,
        "derogaciones": result.derogaciones,
        "cadena_completa": result.cadena_completa,
        "badge": _vigencia_checker.format_vigencia_badge(result),
    }


@router.post("/ingest-norma")
async def ingest_norma_from_source(req: IngestNormaRequest):
    """Ingesta una norma desde una fuente oficial al RAG + grafo de derogaciones."""
    if not _source_router or not _derogation_graph or not _embedder:
        raise HTTPException(503, "Legal system not fully initialized")

    # 1. Obtener texto de la fuente
    norm_data = await _source_router.fetch_norm(
        tipo=req.tipo,
        numero=req.numero,
        anio=req.anio,
        preferred_source=req.source,
    )

    if not norm_data or not norm_data.get("texto_completo"):
        raise HTTPException(404, f"No se encontró {req.tipo} {req.numero} de {req.anio} en las fuentes disponibles")

    # 2. Generar embedding del resumen/título
    embed_text = f"{norm_data.get('titulo', '')} {norm_data.get('texto_completo', '')[:1000]}"
    embeddings = await _embedder.generate_embeddings_batch([embed_text])
    embedding = embeddings[0] if embeddings else None

    # 3. Insertar en tabla normas
    from derogation.models import NormaCreate, FuenteLegal, TipoNorma, EstadoNorma

    norma = NormaCreate(
        tipo=TipoNorma(req.tipo.upper()),
        numero=req.numero,
        anio=req.anio,
        titulo=norm_data.get("titulo"),
        fuente=FuenteLegal(req.source) if req.source in [e.value for e in FuenteLegal] else FuenteLegal.MANUAL,
        fuente_url=norm_data.get("fuente_url"),
        fuente_id=norm_data.get("fuente_id"),
        texto_completo=norm_data.get("texto_completo"),
        sector=norm_data.get("sector"),
        metadata=norm_data.get("metadata", {}),
    )
    norma_id = await _derogation_graph.insert_norma(norma, embedding=embedding)

    # 4. Detectar derogaciones en el texto
    from derogation.detector import detect_derogations
    from derogation.models import DerogacionCreate, TipoDerogacion

    derogations_detected = detect_derogations(norm_data.get("texto_completo", ""))
    derogations_saved = 0

    for det in derogations_detected:
        if det.norma_afectada_numero and det.norma_afectada_anio:
            # Buscar norma afectada en el grafo
            affected = await _derogation_graph.get_norma(
                det.norma_afectada_tipo or req.tipo,
                det.norma_afectada_numero,
                det.norma_afectada_anio,
            )
            if affected:
                derog = DerogacionCreate(
                    norma_origen_id=norma_id,
                    norma_destino_id=str(affected["id"]),
                    tipo=det.tipo_derogacion,
                    articulos_afectados=det.articulos_afectados,
                    fuente_texto=det.texto_fuente,
                    detectado_por="auto_regex",
                    confianza=det.confianza,
                )
                await _derogation_graph.insert_derogacion(derog)
                derogations_saved += 1

    # 5. También ingestar como documento RAG (para búsqueda semántica en chunks)
    chunks_count = 0
    try:
        from ingestion.pipeline import IngestionPipeline
        from config.schema import load_config

        config = load_config()
        pipeline = IngestionPipeline(config, _storage)

        text = norm_data.get("texto_completo", "")
        title = norm_data.get("titulo") or f"{req.tipo} {req.numero} de {req.anio}"
        source_url = norm_data.get("fuente_url") or req.source

        doc_id = await pipeline.ingest_text(
            text=text,
            title=title,
            source=source_url,
            doc_type="legal_norm",
        )
        if doc_id:
            # Count chunks from db
            docs = await _storage.list_documents()
            for d in docs:
                if d["id"] == doc_id:
                    chunks_count = d.get("chunk_count", 0)
                    break
    except Exception as e:
        logger.warning(f"Error ingesting norm as RAG document: {e}")

    return {
        "status": "ok",
        "norma_id": norma_id,
        "titulo": norm_data.get("titulo"),
        "texto_length": len(norm_data.get("texto_completo", "")),
        "derogations_detected": len(derogations_detected),
        "derogations_saved": derogations_saved,
        "rag_chunks": chunks_count,
        "fuente_url": norm_data.get("fuente_url"),
    }


@router.get("/normas")
async def list_normas(
    tipo: Optional[str] = None,
    estado: Optional[str] = None,
    sector: Optional[str] = None,
    limit: int = 50,
):
    """Lista normas indexadas en el grafo."""
    if not _derogation_graph:
        raise HTTPException(503, "Derogation graph not initialized")

    normas = await _derogation_graph.list_normas(tipo=tipo, estado=estado, sector=sector, limit=limit)

    # Serialize dates and UUIDs
    for n in normas:
        for key in ["id", "created_at", "fecha_expedicion"]:
            if key in n and n[key] is not None:
                n[key] = str(n[key])
        if "temas" not in n:
            n["temas"] = []

    return {"normas": normas, "total": len(normas)}


@router.get("/normas/{norma_id}/derogaciones")
async def get_derogaciones(norma_id: str):
    """Obtiene el grafo de derogaciones de una norma."""
    if not _derogation_graph:
        raise HTTPException(503, "Derogation graph not initialized")

    chain = await _derogation_graph.get_derogation_chain(norma_id)

    # Serialize
    for item in chain:
        for key in ["norma_id"]:
            if key in item and item[key] is not None:
                item[key] = str(item[key])

    return {"norma_id": norma_id, "chain": chain}


@router.post("/jurisprudencia/search")
async def search_jurisprudencia(req: JurisprudenciaSearchRequest):
    """Busca jurisprudencia en fuentes vivas."""
    if not _source_router:
        raise HTTPException(503, "Legal sources not initialized")

    results = await _source_router.search_sentencias(
        query=req.query,
        limit=req.limit,
        tipo_sentencia=req.tipo_sentencia,
        desde=req.desde,
    )

    serialized = []
    for r in results:
        serialized.append({
            "source": r.source,
            "tipo": r.tipo,
            "numero": r.numero,
            "anio": r.anio,
            "titulo": r.titulo,
            "estado": r.estado,
            "url": r.url,
            "preview": r.preview,
            "metadata": r.metadata,
        })

    return {"sentencias": serialized, "total": len(serialized)}


@router.get("/jurisprudencia")
async def list_jurisprudencia(
    corte: Optional[str] = None,
    tipo: Optional[str] = None,
    limit: int = 50,
):
    """Lista jurisprudencia indexada localmente."""
    if not _derogation_graph:
        raise HTTPException(503, "Derogation graph not initialized")

    sentencias = await _derogation_graph.list_jurisprudencia(corte=corte, tipo=tipo, limit=limit)

    for s in sentencias:
        for key in ["id", "fecha"]:
            if key in s and s[key] is not None:
                s[key] = str(s[key])

    return {"sentencias": sentencias, "total": len(sentencias)}


@router.get("/health")
async def legal_health():
    """Verifica estado de las fuentes legales."""
    if not _source_router:
        return {"status": "not_initialized", "sources": {}}

    health = await _source_router.check_sources_health()
    return {
        "status": "ok" if any(health.values()) else "degraded",
        "sources": health,
    }
