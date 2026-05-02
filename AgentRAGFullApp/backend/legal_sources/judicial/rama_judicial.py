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
    """Scraper placeholder para el portal real de Rama Judicial.

    Implementación real requiere:
      - cookies de sesión (la web es JavaScript-heavy)
      - headless browser (Playwright) o reverse engineering del API GraphQL
      - cumplimiento de robots.txt y rate limiting

    Este placeholder devuelve [] gracefulmente; en prod debe extenderse
    con la lógica de scraping específica.
    """

    name = "rama_judicial_live"
    description = "Live scraper for consultaprocesos.ramajudicial.gov.co"
    is_live = True

    BASE_URL = "https://consultaprocesos.ramajudicial.gov.co"

    async def poll(
        self,
        expediente: str,
        last_polled_at: Optional[date] = None,
        **kwargs,
    ) -> list[JudicialNotification]:
        # TODO: implementar scraping real con httpx + Playwright si está disponible
        logger.info("RamaJudicialLiveSource.poll · %s · not implemented (returning [])", expediente)
        return []

    async def is_available(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(self.BASE_URL)
                return r.status_code < 500
        except Exception:
            return False
