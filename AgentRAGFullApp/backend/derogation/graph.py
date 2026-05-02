"""
Grafo de derogaciones: CRUD sobre las tablas normas y derogaciones en PostgreSQL.
"""
import json
import logging
from typing import Optional
from .models import (
    NormaCreate, NormaResponse, DerogacionCreate,
    JurisprudenciaCreate, VigenciaResult
)

logger = logging.getLogger(__name__)


class DerogationGraph:
    """Gestiona el grafo de derogaciones en PostgreSQL."""

    def __init__(self, pool):
        """
        Args:
            pool: asyncpg connection pool
        """
        self.pool = pool

    # --- NORMAS ---

    @staticmethod
    def _embedding_to_pg(embedding: Optional[list[float]]) -> Optional[str]:
        """Convert Python list to PostgreSQL vector literal."""
        if embedding is None:
            return None
        return "[" + ",".join(str(x) for x in embedding) + "]"

    async def insert_norma(self, norma: NormaCreate, embedding: Optional[list[float]] = None) -> str:
        """Inserta una norma. Retorna el ID. Si ya existe (tipo+numero+anio), retorna el existente."""
        embedding_str = self._embedding_to_pg(embedding)
        async with self.pool.acquire() as conn:
            # Check if exists
            if norma.numero and norma.anio:
                existing = await conn.fetchrow(
                    "SELECT id FROM normas WHERE tipo = $1 AND numero = $2 AND anio = $3",
                    norma.tipo.value, norma.numero, norma.anio
                )
                if existing:
                    logger.info(f"Norma ya existe: {norma.tipo} {norma.numero} de {norma.anio}")
                    # Update if we have more data
                    if norma.texto_completo:
                        await conn.execute(
                            """UPDATE normas SET texto_completo = $1, resumen = $2,
                               fuente_url = $3, embedding = $4::vector, updated_at = NOW()
                               WHERE id = $5""",
                            norma.texto_completo, norma.resumen,
                            norma.fuente_url, embedding_str, existing["id"]
                        )
                    return str(existing["id"])

            row = await conn.fetchrow(
                """INSERT INTO normas
                   (tipo, numero, anio, titulo, fecha_expedicion, fecha_vigencia, estado,
                    entidad_emisora, sector, fuente, fuente_url, fuente_id,
                    texto_completo, resumen, temas, embedding, metadata)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                   RETURNING id""",
                norma.tipo.value, norma.numero, norma.anio, norma.titulo,
                norma.fecha_expedicion, norma.fecha_vigencia, norma.estado.value,
                norma.entidad_emisora, norma.sector, norma.fuente.value,
                norma.fuente_url, norma.fuente_id,
                norma.texto_completo, norma.resumen, norma.temas,
                embedding_str, json.dumps(norma.metadata) if isinstance(norma.metadata, dict) else norma.metadata
            )
            logger.info(f"Norma insertada: {norma.tipo} {norma.numero} de {norma.anio} -> {row['id']}")
            return str(row["id"])

    async def get_norma(self, tipo: str, numero: int, anio: int) -> Optional[dict]:
        """Busca una norma por tipo, numero y año."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM normas WHERE tipo = $1 AND numero = $2 AND anio = $3",
                tipo.upper(), numero, anio
            )
            return dict(row) if row else None

    async def norma_exists(self, tipo: str, numero: int, anio: int) -> bool:
        """Fast check if a norm exists in the graph (indexed lookup)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM normas WHERE tipo = $1 AND numero = $2 AND anio = $3",
                tipo.upper(), numero, anio
            )
            return row is not None

    async def get_norma_by_id(self, norma_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM normas WHERE id = $1::uuid", norma_id)
            return dict(row) if row else None

    async def list_normas(self, tipo: Optional[str] = None, estado: Optional[str] = None,
                          sector: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Lista normas con filtros opcionales."""
        async with self.pool.acquire() as conn:
            query = "SELECT id, tipo, numero, anio, titulo, estado, fecha_expedicion, fuente_url, sector, temas, created_at FROM normas WHERE 1=1"
            params = []
            idx = 1

            if tipo:
                query += f" AND tipo = ${idx}"
                params.append(tipo.upper())
                idx += 1
            if estado:
                query += f" AND estado = ${idx}"
                params.append(estado.upper())
                idx += 1
            if sector:
                query += f" AND sector = ${idx}"
                params.append(sector)
                idx += 1

            query += f" ORDER BY anio DESC, numero DESC LIMIT ${idx}"
            params.append(limit)

            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def update_norma_estado(self, norma_id: str, estado: str):
        """Actualiza el estado de una norma (VIGENTE, DEROGADA, etc.)."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE normas SET estado = $1, updated_at = NOW() WHERE id = $2::uuid",
                estado.upper(), norma_id
            )

    async def delete_norma(self, norma_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM normas WHERE id = $1::uuid", norma_id)

    # --- DEROGACIONES ---

    async def insert_derogacion(self, derogacion: DerogacionCreate) -> str:
        """Inserta una relación de derogación entre dos normas."""
        async with self.pool.acquire() as conn:
            # Check if exists
            existing = await conn.fetchrow(
                """SELECT id FROM derogaciones
                   WHERE norma_origen_id = $1::uuid AND norma_destino_id = $2::uuid AND tipo = $3""",
                derogacion.norma_origen_id, derogacion.norma_destino_id, derogacion.tipo.value
            )
            if existing:
                return str(existing["id"])

            row = await conn.fetchrow(
                """INSERT INTO derogaciones
                   (norma_origen_id, norma_destino_id, tipo, articulos_afectados,
                    fecha_efecto, fuente_texto, detectado_por, confianza)
                   VALUES ($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8)
                   RETURNING id""",
                derogacion.norma_origen_id, derogacion.norma_destino_id,
                derogacion.tipo.value, derogacion.articulos_afectados,
                derogacion.fecha_efecto, derogacion.fuente_texto,
                derogacion.detectado_por, derogacion.confianza
            )

            # Actualizar estado de la norma destino
            if derogacion.tipo.value in ("DEROGA_TOTAL", "SUSTITUYE"):
                await self.update_norma_estado(derogacion.norma_destino_id, "DEROGADA")
            elif derogacion.tipo.value in ("MODIFICA", "DEROGA_PARCIAL", "ADICIONA"):
                await self.update_norma_estado(derogacion.norma_destino_id, "MODIFICADA")

            logger.info(f"Derogación insertada: {derogacion.tipo} -> {row['id']}")
            return str(row["id"])

    async def get_derogations_for(self, norma_id: str) -> list[dict]:
        """Obtiene todas las derogaciones que afectan a una norma."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT d.*, n.tipo as origen_tipo, n.numero as origen_numero,
                          n.anio as origen_anio, n.titulo as origen_titulo
                   FROM derogaciones d
                   JOIN normas n ON n.id = d.norma_origen_id
                   WHERE d.norma_destino_id = $1::uuid
                   ORDER BY d.fecha_efecto DESC NULLS LAST""",
                norma_id
            )
            return [dict(r) for r in rows]

    # --- VIGENCIA ---

    async def check_vigencia(self, tipo: str, numero: int, anio: int) -> VigenciaResult:
        """Verifica la vigencia de una norma consultando el grafo de derogaciones."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM check_vigencia($1, $2, $3)",
                tipo.upper(), numero, anio
            )

            if not rows:
                return VigenciaResult(
                    tipo=tipo.upper(), numero=numero, anio=anio,
                    estado="DESCONOCIDA", encontrada=False,
                    titulo=None
                )

            first = rows[0]
            derogaciones = []
            for row in rows:
                if row["derogada_por_tipo"]:
                    derogaciones.append({
                        "tipo_derogacion": row["tipo_derogacion"],
                        "norma_tipo": row["derogada_por_tipo"],
                        "norma_numero": row["derogada_por_numero"],
                        "norma_anio": row["derogada_por_anio"],
                        "norma_titulo": row["derogada_por_titulo"],
                        "fecha_efecto": str(row["fecha_efecto"]) if row["fecha_efecto"] else None,
                        "articulos_afectados": row["articulos_afectados"] or [],
                    })

            return VigenciaResult(
                norma_id=str(first["norma_id"]),
                tipo=first["tipo"],
                numero=first["numero"],
                anio=first["anio"],
                titulo=first["titulo"],
                estado=first["estado"],
                encontrada=True,
                derogaciones=derogaciones,
            )

    async def get_derogation_chain(self, norma_id: str, direction: str = "both") -> list[dict]:
        """Obtiene la cadena completa de derogaciones recursivamente."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM get_derogation_chain($1::uuid, $2)",
                norma_id, direction
            )
            return [dict(r) for r in rows]

    # --- JURISPRUDENCIA ---

    async def insert_jurisprudencia(self, sentencia: JurisprudenciaCreate,
                                     embedding: Optional[list[float]] = None) -> str:
        """Inserta una sentencia de jurisprudencia."""
        embedding_str = self._embedding_to_pg(embedding)
        async with self.pool.acquire() as conn:
            # Check if exists
            if sentencia.numero:
                existing = await conn.fetchrow(
                    "SELECT id FROM jurisprudencia WHERE corte = $1 AND numero = $2",
                    sentencia.corte.value, sentencia.numero
                )
                if existing:
                    return str(existing["id"])

            row = await conn.fetchrow(
                """INSERT INTO jurisprudencia
                   (corte, sala, tipo_sentencia, numero, expediente, fecha, magistrado,
                    temas, es_precedente, normas_interpretadas, ratio_decidendi,
                    obiter_dicta, decision, fuente_url, fuente, texto_completo, embedding, metadata)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                   RETURNING id""",
                sentencia.corte.value, sentencia.sala,
                sentencia.tipo_sentencia.value if sentencia.tipo_sentencia else None,
                sentencia.numero, sentencia.expediente, sentencia.fecha,
                sentencia.magistrado, sentencia.temas, sentencia.es_precedente,
                sentencia.normas_interpretadas, sentencia.ratio_decidendi,
                sentencia.obiter_dicta, sentencia.decision, sentencia.fuente_url,
                sentencia.fuente.value, sentencia.texto_completo, embedding_str,
                json.dumps(sentencia.metadata) if isinstance(sentencia.metadata, dict) else sentencia.metadata
            )
            return str(row["id"])

    async def list_jurisprudencia(self, corte: Optional[str] = None,
                                   tipo: Optional[str] = None,
                                   limit: int = 50) -> list[dict]:
        async with self.pool.acquire() as conn:
            query = "SELECT id, corte, tipo_sentencia, numero, fecha, magistrado, temas, es_precedente, decision, fuente_url FROM jurisprudencia WHERE 1=1"
            params = []
            idx = 1

            if corte:
                query += f" AND corte = ${idx}"
                params.append(corte.upper())
                idx += 1
            if tipo:
                query += f" AND tipo_sentencia = ${idx}"
                params.append(tipo.upper())
                idx += 1

            query += f" ORDER BY fecha DESC NULLS LAST LIMIT ${idx}"
            params.append(limit)

            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]
