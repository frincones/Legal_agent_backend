"""DOF Colombia · Diario Oficial — RSS / search by keywords.

For demo purposes, generates a small set of plausible DOF entries based
on keywords (e.g., "reforma laboral", "hábeas data"). Real implementation
would scrape https://www.imprenta.gov.co/diario-oficial.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta
from typing import Optional

from .base import BaseJudicialSource, JudicialNotification

logger = logging.getLogger(__name__)


class DofCoDemoSource(BaseJudicialSource):
    """DOF demo source. Returns 0..1 notifications per call based on keyword.

    Used when the firm subscribes to topic-based DOF alerts (not expediente).
    """

    name = "dof_co_demo"
    description = "Diario Oficial Colombia · demo source"
    is_live = False

    PLANTILLAS = [
        ("decreto", "Decreto 0123 de 2026 · reforma al CST", "alta"),
        ("ley", "Ley 2400 de 2026 · habeas data", "alta"),
        ("circular", "Circular SIC 0089 de 2026 · marcas no tradicionales", "info"),
        ("sentencia", "C-100/2026 · constitucionalidad reforma laboral", "critica"),
    ]

    async def poll(
        self,
        expediente: str,
        last_polled_at: Optional[date] = None,
        **kwargs,
    ) -> list[JudicialNotification]:
        # En DOF, "expediente" es keyword/tema. ej "reforma_laboral"
        if not expediente:
            return []
        seed = hashlib.sha256(f"dof|{expediente}|{date.today().isoformat()}".encode()).digest()
        if seed[0] % 4 != 0:  # ~25% chance de notif por tema cada día
            return []
        idx = seed[1] % len(self.PLANTILLAS)
        tipo, titulo, severidad = self.PLANTILLAS[idx]
        fecha = date.today() - timedelta(days=seed[2] % 3)
        return [
            JudicialNotification(
                fuente=self.name,
                titulo=titulo,
                fecha_publicacion=fecha,
                fecha_actuacion=fecha,
                expediente=expediente,
                tipo=tipo,
                severidad=severidad,
                resumen=f"Publicación oficial DOF relacionada con '{expediente}'.",
                url_oficial="https://www.imprenta.gov.co/diario-oficial",
                metadata={"demo": True, "tema": expediente},
            )
        ]
