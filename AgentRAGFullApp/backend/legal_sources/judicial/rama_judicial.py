"""Rama Judicial Colombia · poller para actuaciones en SAMAI / Consulta de Procesos.

Implementación dual:
  - LiveSource: scraping del portal público (https://consultaprocesos.ramajudicial.gov.co)
  - DemoSource: genera notificaciones simuladas para los expedientes seeded
    (determinístico, sin red, reproducible para testing)

El scraping en vivo es brittle (la web puede cambiar). Para demo y CI usamos
DemoSource. En producción, el orquestador (judicial_poller.py) puede combinar
ambos: si LiveSource falla, hace fallback al simulator vía settings.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta
from typing import Optional

from .base import BaseJudicialSource, JudicialNotification

logger = logging.getLogger(__name__)


class RamaJudicialDemoSource(BaseJudicialSource):
    """Genera notificaciones determinísticas basadas en el expediente.

    Útil para:
      - Demo (output predecible)
      - CI / golden tests
      - Fallback cuando el scraper en vivo falla

    El resultado se computa con un hash del expediente + fecha actual,
    así cada día produce 0..2 notificaciones plausibles.
    """

    name = "rama_judicial_demo"
    description = "Demo deterministic source for Colombian judicial notifications"
    is_live = False

    PLANTILLAS = [
        ("auto", "Auto admite demanda", "alta"),
        ("auto", "Auto resuelve recurso de reposición", "info"),
        ("auto", "Auto fija fecha de audiencia inicial Art. 372 CGP", "critica"),
        ("notificacion", "Notificación por aviso (CGP Art. 292)", "alta"),
        ("oficio", "Oficio comisorio expedido", "info"),
        ("sentencia", "Sentencia de primera instancia", "critica"),
        ("edicto", "Edicto de notificación", "info"),
        ("auto", "Auto corre traslado contestación", "alta"),
        ("auto", "Auto decreta pruebas", "alta"),
    ]

    JUZGADOS = [
        "Juzgado 12 Laboral del Circuito de Bogotá",
        "Juzgado 18 Civil Municipal de Bogotá",
        "Juzgado 5 de Familia de Bogotá",
        "Tribunal Superior de Bogotá · Sala Civil",
    ]

    async def poll(
        self,
        expediente: str,
        last_polled_at: Optional[date] = None,
        **kwargs,
    ) -> list[JudicialNotification]:
        if not expediente:
            return []

        seed_hash = hashlib.sha256(
            f"{expediente}|{date.today().isoformat()}".encode()
        ).digest()
        # 1..2 notifications per poll (always at least 1 in demo mode for
        # predictable UX). Real source can produce 0 when no novedad.
        n = 1 + (seed_hash[0] % 2)
        notifs: list[JudicialNotification] = []
        for i in range(n):
            tpl_idx = seed_hash[1 + i] % len(self.PLANTILLAS)
            jzg_idx = seed_hash[3 + i] % len(self.JUZGADOS)
            tipo, titulo, severidad = self.PLANTILLAS[tpl_idx]
            days_ago = seed_hash[5 + i] % 4  # actuación 0-3 días atrás
            fecha_pub = date.today() - timedelta(days=days_ago)
            if last_polled_at and fecha_pub <= last_polled_at:
                continue
            notif = JudicialNotification(
                fuente=self.name,
                titulo=f"{titulo} · Exp. {expediente}",
                fecha_publicacion=fecha_pub,
                fecha_actuacion=fecha_pub,
                expediente=expediente,
                juzgado=self.JUZGADOS[jzg_idx],
                tipo=tipo,
                severidad=severidad,
                resumen=f"Actuación procesal automática para el expediente {expediente}.",
                url_oficial=(
                    "https://consultaprocesos.ramajudicial.gov.co/Procesos/"
                    f"NumeroRadicacion?numero={expediente}"
                ),
                metadata={"demo": True, "generated": "deterministic"},
            )
            notifs.append(notif)
        return notifs


class RamaJudicialLiveSource(BaseJudicialSource):
    """Scraper para la API pública de consulta de procesos de la Rama Judicial.

    Endpoint público (sin auth): /api/v2/Procesos/Consulta/NumeroRadicacion
      query: {numero, SoloActivos, pagina, cantidadRegistros}
    Detalle de actuaciones por idProceso:
      /api/v2/Proceso/Actuaciones/{idProceso}

    Tolerante a fallos: si el endpoint cambia o la red falla, devuelve [] y
    el orquestador hace fallback al simulator.
    """

    name = "rama_judicial_live"
    description = "Live scraper for consultaprocesos.ramajudicial.gov.co (API v2 público)"
    is_live = True

    BASE_URL = "https://consultaprocesos.ramajudicial.gov.co"
    API_CONSULTA = BASE_URL + "/api/v2/Procesos/Consulta/NumeroRadicacion"
    API_ACTUACIONES = BASE_URL + "/api/v2/Proceso/Actuaciones"
    HEADERS = {
        "User-Agent": "LexAI/1.0 (+https://lexai.co/about) httpx",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CO,es;q=0.9",
    }
    TIMEOUT = 10.0

    SEVERIDAD_BY_TIPO = {
        "auto admite demanda": "alta",
        "auto fija fecha de audiencia": "critica",
        "sentencia": "critica",
        "auto resuelve recurso": "alta",
        "notificación por aviso": "alta",
        "edicto": "info",
        "oficio": "info",
        "auto corre traslado": "alta",
        "auto decreta pruebas": "alta",
    }

    async def poll(
        self,
        expediente: str,
        last_polled_at: Optional[date] = None,
        **kwargs,
    ) -> list[JudicialNotification]:
        if not expediente:
            return []
        try:
            import httpx
            async with httpx.AsyncClient(
                timeout=self.TIMEOUT, headers=self.HEADERS, follow_redirects=True
            ) as client:
                # 1) Resolver idProceso por número de radicación.
                params = {
                    "numero": expediente,
                    "SoloActivos": "false",
                    "pagina": 1,
                    "cantidadRegistros": 1,
                }
                r = await client.get(self.API_CONSULTA, params=params)
                if r.status_code != 200:
                    logger.info("rama_judicial · consulta %s · HTTP %s", expediente, r.status_code)
                    return []
                data = r.json() or {}
                procesos = data.get("procesos") or []
                if not procesos:
                    return []
                proc = procesos[0]
                id_proceso = proc.get("idProceso") or proc.get("IdProceso")
                if not id_proceso:
                    return []
                juzgado = proc.get("despacho") or proc.get("Despacho") or ""
                # 2) Obtener actuaciones.
                r2 = await client.get(
                    f"{self.API_ACTUACIONES}/{id_proceso}",
                    params={"pagina": 1, "cantidadRegistros": 50},
                )
                if r2.status_code != 200:
                    logger.info("rama_judicial · actuaciones %s · HTTP %s", id_proceso, r2.status_code)
                    return []
                act_data = r2.json() or {}
                actuaciones = act_data.get("actuaciones") or []
        except Exception as e:
            logger.warning("rama_judicial scraper failed: %s", e)
            return []

        notifs: list[JudicialNotification] = []
        for a in actuaciones:
            try:
                fecha_str = a.get("fechaActuacion") or a.get("fechaRegistro")
                if not fecha_str:
                    continue
                fa = self._parse_date(fecha_str)
                if not fa:
                    continue
                if last_polled_at and fa <= last_polled_at:
                    continue
                titulo = a.get("actuacion") or a.get("anotacion") or "Actuación"
                tipo, severidad = self._classify(titulo)
                resumen = a.get("anotacion") or titulo
                notifs.append(
                    JudicialNotification(
                        fuente=self.name,
                        titulo=str(titulo)[:280],
                        fecha_publicacion=fa,
                        fecha_actuacion=fa,
                        expediente=expediente,
                        juzgado=str(juzgado)[:200] if juzgado else None,
                        tipo=tipo,
                        severidad=severidad,
                        resumen=str(resumen)[:1000] if resumen else None,
                        url_oficial=(
                            f"{self.BASE_URL}/Procesos/NumeroRadicacion?numero={expediente}"
                        ),
                        metadata={"idProceso": id_proceso, "raw": a},
                    )
                )
            except Exception as e:
                logger.debug("skip actuacion: %s", e)
        return notifs

    def _parse_date(self, s: str) -> Optional[date]:
        s = (s or "").strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                from datetime import datetime as _dt
                return _dt.strptime(s.split("Z")[0], fmt).date()
            except Exception:
                continue
        return None

    def _classify(self, titulo: str) -> tuple[str, str]:
        t = (titulo or "").lower()
        sev = "info"
        for k, v in self.SEVERIDAD_BY_TIPO.items():
            if k in t:
                sev = v
                break
        if "sentencia" in t:
            return "sentencia", sev
        if "edicto" in t:
            return "edicto", sev
        if "oficio" in t:
            return "oficio", sev
        if "notific" in t:
            return "notificacion", sev
        if "auto" in t:
            return "auto", sev
        return "otro", sev

    async def is_available(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(self.BASE_URL)
                return r.status_code < 500
        except Exception:
            return False
