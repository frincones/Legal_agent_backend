"""
Verificador de vigencia de normas legales.
Combina el grafo local con búsqueda en fuentes vivas.
"""
import logging
from typing import Optional
from .graph import DerogationGraph
from .models import VigenciaResult

logger = logging.getLogger(__name__)


class VigenciaChecker:
    """
    Verifica si una norma está vigente, derogada o modificada.
    Primero consulta el grafo local, opcionalmente consulta fuentes vivas.
    """

    def __init__(self, graph: DerogationGraph):
        self.graph = graph

    async def check(self, tipo: str, numero: int, anio: int,
                    check_live_sources: bool = False) -> VigenciaResult:
        """
        Verifica la vigencia de una norma.

        Args:
            tipo: Tipo de norma (LEY, DECRETO, RESOLUCION, etc.)
            numero: Número de la norma
            anio: Año de expedición
            check_live_sources: Si True, consulta fuentes vivas si no está en el grafo

        Returns:
            VigenciaResult con estado y cadena de derogaciones
        """
        # 1. Consultar grafo local
        result = await self.graph.check_vigencia(tipo, numero, anio)

        if result.encontrada:
            # Si encontramos la norma y tiene derogaciones, obtener cadena completa
            if result.derogaciones and result.norma_id:
                chain = await self.graph.get_derogation_chain(result.norma_id)
                result.cadena_completa = chain
            return result

        # 2. Si no está en el grafo y se permite, buscar en fuentes vivas
        if check_live_sources:
            live_result = await self._check_live_sources(tipo, numero, anio)
            if live_result:
                return live_result

        # 3. No encontrada en ningún lado
        return VigenciaResult(
            tipo=tipo.upper(),
            numero=numero,
            anio=anio,
            estado="NO_INDEXADA",
            encontrada=False,
            titulo=None,
        )

    async def _check_live_sources(self, tipo: str, numero: int, anio: int) -> Optional[VigenciaResult]:
        """
        Consulta fuentes vivas para verificar vigencia.
        Se importa lazy para evitar dependencia circular.
        """
        try:
            from legal_sources.datos_gov_co import DatosGovCoSource
            source = DatosGovCoSource()
            results = await source.search_norma(tipo, numero, anio)
            if results:
                first = results[0]
                vigencia = first.get("vigencia", "").upper()
                estado = "VIGENTE" if "VIGENTE" in vigencia else "DEROGADA" if "DEROGAD" in vigencia else "DESCONOCIDA"
                return VigenciaResult(
                    tipo=tipo.upper(),
                    numero=numero,
                    anio=anio,
                    titulo=first.get("titulo"),
                    estado=estado,
                    encontrada=True,
                )
        except Exception as e:
            logger.warning(f"Error consultando fuentes vivas para vigencia: {e}")

        return None

    async def verify_results(self, normas_mencionadas: list[dict]) -> list[VigenciaResult]:
        """
        Verifica la vigencia de múltiples normas mencionadas en un contexto.

        Args:
            normas_mencionadas: Lista de dicts con keys 'tipo', 'numero', 'anio'

        Returns:
            Lista de VigenciaResult
        """
        results = []
        for norma in normas_mencionadas:
            tipo = norma.get("tipo", "")
            numero = norma.get("numero")
            anio = norma.get("anio")
            if tipo and numero and anio:
                result = await self.check(tipo, int(numero), int(anio))
                results.append(result)
        return results

    def format_vigencia_badge(self, result: VigenciaResult) -> str:
        """Formatea el resultado de vigencia como badge para incluir en respuestas."""
        nombre = f"{result.tipo} {result.numero or ''} de {result.anio or ''}"

        if not result.encontrada:
            return f"⚠️ {nombre} — Vigencia no verificada (no indexada)"

        if result.estado == "VIGENTE":
            return f"✅ {nombre} — VIGENTE"
        elif result.estado == "DEROGADA":
            derogada_info = ""
            if result.derogaciones:
                d = result.derogaciones[0]
                derogada_info = f" por {d.get('norma_tipo', '')} {d.get('norma_numero', '')} de {d.get('norma_anio', '')}"
            return f"❌ {nombre} — DEROGADA{derogada_info}"
        elif result.estado == "MODIFICADA":
            return f"⚠️ {nombre} — MODIFICADA (verificar artículos específicos)"
        else:
            return f"❓ {nombre} — Estado: {result.estado}"
