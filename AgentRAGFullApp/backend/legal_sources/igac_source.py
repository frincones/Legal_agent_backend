"""Sprint L8 - IGAC catastral verifier.

Verifica cedulas catastrales colombianas contra:
  1. Tabla gestores_catastrales (DIVIPOLA -> gestor IGAC u local). Esto valida
     que el codigo municipal del cedula corresponde a un municipio real y
     dice quien es la autoridad catastral.
  2. Cache predios_cache si ya se consulto antes la cedula.
  3. Para Bogota (DIVIPOLA 11001), llamamos al servicio publico de mapas.bogota.gov.co
     (IDECA) para enriquecer con direccion + area.

NO requiere credenciales. No hace web scraping con captcha. Si IGAC nacional
no expone API publico para la cedula individual, devolvemos solo metadata del
gestor + validacion estructural.

Formato cedula catastral colombiana (NUNCA estandar al 100%):
  - Antiguo: 5-23 caracteres alfanumericos
  - Nuevo (resolucion 70/2011): 30 digitos
    DD MMM SS PP MM PRED CC EE PP   ej. 250010000010 14 0001 0000 0000

Parseo basico: primeros 5 digitos son siempre DIVIPOLA municipio
  (departamento[2] + municipio[3]).
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Any

import httpx

logger = logging.getLogger(__name__)

# Regex para cedula catastral: secuencia de digitos de longitud variable.
# Aceptamos 7-30 chars alfanumericos (predomina digitos pero algunos antiguos
# tienen letras de manzana). El primer subgrupo de 5 digitos es DIVIPOLA.
_CEDULA_PATTERN = re.compile(r"^\s*(\d{5})\s*[-\s]?\s*([A-Z0-9\s\-]+)\s*$", re.IGNORECASE)

# Bogota IDECA - servicio publico de geocodificacion de la UAECD
# Aunque las consultas individuales requieren captcha en el portal web,
# el servicio mapas.bogota.gov.co/arcgis tiene endpoints publicos REST.
BOGOTA_GEO_BASE = "https://serviciosgis.catastrobogota.gov.co/arcgis/rest/services"


def parse_cedula_catastral(raw: str) -> Optional[dict[str, str]]:
    """Extrae DIVIPOLA y resto del cedula catastral.

    Returns dict {divipola, resto, raw, longitud} o None si no parsea.
    """
    if not raw:
        return None
    s = raw.strip().replace(" ", "").replace("-", "").upper()
    if len(s) < 5 or not s[:5].isdigit():
        return None
    divipola = s[:5]
    resto = s[5:]
    return {
        "divipola": divipola,
        "resto": resto,
        "raw": raw.strip(),
        "canonical": s,
        "longitud": len(s),
    }


async def verify_cedula_estructura(pool, raw: str) -> dict[str, Any]:
    """Verificacion ESTRUCTURAL: existe el DIVIPOLA en gestores_catastrales?

    Returns:
      {
        estado: 'valida' | 'divipola_invalido' | 'unparseable',
        cedula_canonical: str,
        divipola: str,
        municipio: str,
        departamento: str,
        gestor_cat: str,
        url_consulta: str,    # link al portal del gestor
        fuente: 'estructura_only',
      }
    """
    parsed = parse_cedula_catastral(raw)
    if not parsed:
        return {
            "estado": "unparseable",
            "raw": raw,
            "error": "cedula no parseable (esperado: 5+ digitos)",
        }

    divipola = parsed["divipola"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select divipola, municipio, departamento, gestor_cat, gestor_con,
                   estado_act, area_km2
              from gestores_catastrales
             where divipola = $1
             limit 1
            """,
            divipola,
        )

    if not row:
        return {
            "estado": "divipola_invalido",
            "cedula_canonical": parsed["canonical"],
            "divipola": divipola,
            "raw": raw,
            "error": f"DIVIPOLA {divipola} no encontrado en gestores catastrales (1,121 municipios)",
        }

    gestor = row["gestor_cat"] or "IGAC"
    url_map = {
        "IGAC": "https://www.igac.gov.co/es/contenido/areas-estrategicas/catastro",
        "CATASTRO BOGOTA": "https://www.catastrobogota.gov.co/consulta-de-predio",
        "CATASTRO ANTIOQUIA": "https://catastro.antioquia.gov.co",
        "UNIDAD ADMINISTRATIVA ESPECIAL DE CATASTRO DISTRITAL BOGOTA UAECD":
            "https://www.catastrobogota.gov.co",
        "CATASTRO MUNICIPAL DE CALI": "https://www.cali.gov.co/catastro",
    }
    url_consulta = url_map.get(gestor, "https://www.igac.gov.co")

    return {
        "estado": "valida",
        "cedula_canonical": parsed["canonical"],
        "divipola": divipola,
        "municipio": row["municipio"],
        "departamento": row["departamento"],
        "gestor_cat": gestor,
        "gestor_estado": row["estado_act"],
        "area_km2_municipio": float(row["area_km2"]) if row["area_km2"] else None,
        "longitud_cedula": parsed["longitud"],
        "url_consulta": url_consulta,
        "fuente": "estructura_only",
    }


async def fetch_bogota_predio(cedula_canonical: str, timeout: float = 8.0) -> Optional[dict[str, Any]]:
    """Consulta opcional al servicio IDECA de Bogota.

    Solo se intenta si el cedula empieza con 11001. El servicio publico
    de mapas.bogota.gov.co (ArcGIS REST) tiene varias capas; la mas util
    es 'Cartografia_Basica/MapServer/5' que tiene polygonos de predios.

    Si responde algo util, retorna {direccion, area_terreno, area_construccion}.
    Si falla por cualquier razon, retorna None (caller debe degradarse).
    """
    if not cedula_canonical.startswith("11001"):
        return None

    # Query layer publico de Catastro Bogota (CHIP busqueda por cedula catastral)
    url = (
        f"{BOGOTA_GEO_BASE}/Catastro/MapServer/0/query"
        f"?where=CODIGO_HOMOLOGADO%3D%27{cedula_canonical}%27"
        f"&outFields=*&returnGeometry=false&f=json"
    )
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None
            data = r.json()
            features = data.get("features", [])
            if not features:
                return None
            attrs = features[0].get("attributes", {})
            return {
                "fuente": "catastro_bogota",
                "direccion": attrs.get("DIRECCION") or attrs.get("DIRECCION_VIA"),
                "area_terreno": attrs.get("AREA_TERRENO"),
                "area_construccion": attrs.get("AREA_CONSTRUIDA"),
                "matricula_inmobiliaria": attrs.get("MATRICULA"),
                "destino_economico": attrs.get("DESTINO"),
                "raw": attrs,
            }
    except Exception as e:
        logger.warning("fetch_bogota_predio failed for %s: %s", cedula_canonical, e)
        return None


async def verify_cedula_full(pool, raw: str) -> dict[str, Any]:
    """Verificacion completa: estructura + cache + enriquecimiento Bogota.

    Chain:
      1. parse + lookup gestores -> estructura valida si DIVIPOLA existe
      2. lookup predios_cache -> si ya se consulto, devolver cache
      3. si Bogota -> fetch IDECA + persistir en predios_cache
      4. otro caso -> retornar solo estructura
    """
    estructura = await verify_cedula_estructura(pool, raw)
    if estructura["estado"] != "valida":
        return estructura

    cedula = estructura["cedula_canonical"]

    # Cache hit?
    async with pool.acquire() as conn:
        cache_row = await conn.fetchrow(
            """
            select cedula_catastral, direccion, area_terreno, area_construccion,
                   destino_economico, matricula_inmobiliaria, fuente, verified_at
              from predios_cache
             where cedula_catastral = $1
             limit 1
            """,
            cedula,
        )

    if cache_row:
        return {
            **estructura,
            "estado": "verificada_cache",
            "direccion": cache_row["direccion"],
            "area_terreno": float(cache_row["area_terreno"]) if cache_row["area_terreno"] else None,
            "area_construccion": float(cache_row["area_construccion"]) if cache_row["area_construccion"] else None,
            "destino_economico": cache_row["destino_economico"],
            "matricula_inmobiliaria": cache_row["matricula_inmobiliaria"],
            "fuente": cache_row["fuente"],
        }

    # Bogota -> intentar enriquecimiento
    bog = await fetch_bogota_predio(cedula)
    if bog:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into predios_cache
                      (cedula_catastral, divipola, municipio, direccion,
                       area_terreno, area_construccion, destino_economico,
                       matricula_inmobiliaria, fuente, fuente_url, verified_at)
                    values
                      ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
                    on conflict (cedula_catastral) do update set
                      direccion = excluded.direccion,
                      area_terreno = excluded.area_terreno,
                      area_construccion = excluded.area_construccion,
                      destino_economico = excluded.destino_economico,
                      matricula_inmobiliaria = excluded.matricula_inmobiliaria,
                      verified_at = now()
                    """,
                    cedula, estructura["divipola"], estructura["municipio"],
                    bog.get("direccion"),
                    bog.get("area_terreno"), bog.get("area_construccion"),
                    bog.get("destino_economico"), bog.get("matricula_inmobiliaria"),
                    "catastro_bogota",
                    "https://www.catastrobogota.gov.co",
                )
        except Exception as e:
            logger.warning("persist predio cache failed: %s", e)

        return {
            **estructura,
            "estado": "verificada_live",
            "direccion": bog.get("direccion"),
            "area_terreno": bog.get("area_terreno"),
            "area_construccion": bog.get("area_construccion"),
            "destino_economico": bog.get("destino_economico"),
            "matricula_inmobiliaria": bog.get("matricula_inmobiliaria"),
            "fuente": "catastro_bogota",
        }

    # Solo estructura, sin enriquecimiento
    return {**estructura, "estado": "estructura_valida"}
