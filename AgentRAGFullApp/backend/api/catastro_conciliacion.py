"""Sprint L8 + L10 - Endpoints publicos para:
  - /v1/predios/verify         IGAC + Catastro Bogota
  - /v1/conciliacion/verify    Centros conciliacion SICAAC
  - /v1/conciliacion/search    Buscar centros por ciudad/entidad

Ninguno requiere credenciales externas. Todos consultan tablas locales
sembradas desde datos.gov.co + scraping puntual.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.db import get_storage
from legal_sources.igac_source import verify_cedula_full

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["catastro_conciliacion"])


# ---------- Predios (Sprint L8) ----------

class PredioVerifyRequest(BaseModel):
    cedula_catastral: str = Field(..., min_length=5, max_length=40)


class PredioVerifyResponse(BaseModel):
    estado: str                                 # valida | divipola_invalido | unparseable | verificada_cache | verificada_live | estructura_valida
    cedula_canonical: Optional[str] = None
    divipola: Optional[str] = None
    municipio: Optional[str] = None
    departamento: Optional[str] = None
    gestor_cat: Optional[str] = None
    gestor_estado: Optional[str] = None
    direccion: Optional[str] = None
    area_terreno: Optional[float] = None
    area_construccion: Optional[float] = None
    destino_economico: Optional[str] = None
    matricula_inmobiliaria: Optional[str] = None
    url_consulta: Optional[str] = None
    longitud_cedula: Optional[int] = None
    fuente: Optional[str] = None
    error: Optional[str] = None


@router.post("/predios/verify", response_model=PredioVerifyResponse)
async def predio_verify(
    req: PredioVerifyRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Verifica una cedula catastral colombiana.

    Cadena:
      1. Parse + lookup DIVIPOLA en gestores_catastrales (1,121 municipios).
      2. Si existe el municipio -> identifica el gestor (IGAC o local).
      3. Si Bogota -> intenta enriquecer con IDECA (mapas.bogota.gov.co).
      4. Retorna metadata + url del portal del gestor para consulta manual.
    """
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage not available")
    result = await verify_cedula_full(storage.pool, req.cedula_catastral)
    return PredioVerifyResponse(**result)


# ---------- Conciliacion (Sprint L10) ----------

class CentroResult(BaseModel):
    id: str
    nombre: str
    numero_resolucion: Optional[str] = None
    entidad_origen: Optional[str] = None
    ciudad: Optional[str] = None
    departamento: Optional[str] = None
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    estado: str
    url_oficial: Optional[str] = None
    fuente_url: str = "https://sicaac.gov.co"


class CentrosSearchResponse(BaseModel):
    total: int
    centros: list[CentroResult]
    query: dict


@router.get("/conciliacion/search", response_model=CentrosSearchResponse)
async def centros_search(
    ciudad: Optional[str] = Query(None, max_length=100),
    entidad: Optional[str] = Query(None, max_length=100),
    nombre: Optional[str] = Query(None, max_length=100),
    estado: Optional[str] = Query("autorizado"),
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(get_current_firm),
):
    """Busca centros de conciliacion autorizados por Minjusticia.

    Filtros (todos opcionales, AND):
      - ciudad: 'Bogota', 'Medellin', etc. (ILIKE)
      - entidad: 'Camara de Comercio', 'Notaria', etc.
      - nombre: substring match
      - estado: autorizado (default) | suspendido | cancelado | vencido
    """
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage not available")

    wheres = ["true"]
    params: list = []
    idx = 1
    if ciudad:
        wheres.append(f"ciudad ilike ${idx}")
        params.append(f"%{ciudad}%")
        idx += 1
    if entidad:
        wheres.append(f"entidad_origen ilike ${idx}")
        params.append(f"%{entidad}%")
        idx += 1
    if nombre:
        wheres.append(f"nombre ilike ${idx}")
        params.append(f"%{nombre}%")
        idx += 1
    if estado:
        wheres.append(f"estado = ${idx}")
        params.append(estado)
        idx += 1
    params.append(limit)

    sql = f"""
        select id, nombre, numero_resolucion, entidad_origen, ciudad, departamento,
               direccion, telefono, email, estado, url_oficial
          from centros_conciliacion
         where {' and '.join(wheres)}
         order by case when ciudad ilike 'bogot%' then 0 else 1 end, nombre
         limit ${idx}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    centros = [
        CentroResult(
            id=str(r["id"]),
            nombre=r["nombre"],
            numero_resolucion=r["numero_resolucion"],
            entidad_origen=r["entidad_origen"],
            ciudad=r["ciudad"],
            departamento=r["departamento"],
            direccion=r["direccion"],
            telefono=r["telefono"],
            email=r["email"],
            estado=r["estado"],
            url_oficial=r["url_oficial"],
        )
        for r in rows
    ]
    return CentrosSearchResponse(
        total=len(centros),
        centros=centros,
        query={"ciudad": ciudad, "entidad": entidad, "nombre": nombre, "estado": estado},
    )


class CentroVerifyRequest(BaseModel):
    nombre: str = Field(..., min_length=3, max_length=200)
    ciudad: Optional[str] = Field(None, max_length=100)


class CentroVerifyResponse(BaseModel):
    estado: str                          # 'autorizado' | 'no_encontrado' | 'cancelado' | 'suspendido'
    match: Optional[CentroResult] = None
    similares: list[CentroResult] = []


@router.post("/conciliacion/verify", response_model=CentroVerifyResponse)
async def centro_verify(
    req: CentroVerifyRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Verifica si un centro de conciliacion existe y esta autorizado.

    Match prioritario: nombre + ciudad. Si no exact match, devuelve top-5
    similares (ILIKE) para que el abogado revise. Tipico caso: cita
    'Centro de Conciliacion Camara de Comercio' (sin ciudad) y la respuesta
    sugiere 'Centro de Conciliacion Camara de Comercio de Bogota'.
    """
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage not available")

    async with storage.pool.acquire() as conn:
        # Exact match (case-insensitive)
        if req.ciudad:
            exact = await conn.fetchrow(
                """
                select id, nombre, numero_resolucion, entidad_origen, ciudad,
                       departamento, direccion, telefono, email, estado, url_oficial
                  from centros_conciliacion
                 where lower(nombre) = lower($1) and lower(ciudad) = lower($2)
                 limit 1
                """,
                req.nombre, req.ciudad,
            )
        else:
            exact = await conn.fetchrow(
                """
                select id, nombre, numero_resolucion, entidad_origen, ciudad,
                       departamento, direccion, telefono, email, estado, url_oficial
                  from centros_conciliacion
                 where lower(nombre) = lower($1)
                 limit 1
                """,
                req.nombre,
            )

        if exact:
            return CentroVerifyResponse(
                estado=exact["estado"],
                match=CentroResult(
                    id=str(exact["id"]), nombre=exact["nombre"],
                    numero_resolucion=exact["numero_resolucion"],
                    entidad_origen=exact["entidad_origen"],
                    ciudad=exact["ciudad"], departamento=exact["departamento"],
                    direccion=exact["direccion"], telefono=exact["telefono"],
                    email=exact["email"], estado=exact["estado"],
                    url_oficial=exact["url_oficial"],
                ),
            )

        # Fuzzy ILIKE on nombre + ciudad
        similares_rows = await conn.fetch(
            """
            select id, nombre, numero_resolucion, entidad_origen, ciudad,
                   departamento, direccion, telefono, email, estado, url_oficial
              from centros_conciliacion
             where nombre ilike $1
                or ($2 <> '' and ciudad ilike $2)
             order by case when ciudad ilike $2 then 0 else 1 end,
                      length(nombre)
             limit 5
            """,
            f"%{req.nombre}%", f"%{req.ciudad or ''}%",
        )

    similares = [
        CentroResult(
            id=str(r["id"]), nombre=r["nombre"],
            numero_resolucion=r["numero_resolucion"],
            entidad_origen=r["entidad_origen"],
            ciudad=r["ciudad"], departamento=r["departamento"],
            direccion=r["direccion"], telefono=r["telefono"],
            email=r["email"], estado=r["estado"],
            url_oficial=r["url_oficial"],
        )
        for r in similares_rows
    ]
    return CentroVerifyResponse(estado="no_encontrado", similares=similares)
