"""Sprint 21 · Identity providers · validación cruzada.

Arquitectura modular:
  · MockProvider · genera respuestas deterministas (por hash del id_value).
                   Siempre disponible. Útil para demo, dev, tests.
  · HttpProvider · llama APIs reales si las env vars correspondientes están
                   configuradas (REGISTRO_CIVIL_API_KEY, RUE_API_KEY,
                   RUT_API_KEY + BASE URLs). Sin esas vars, falla con
                   `not_configured` y caemos al Mock.

Cada provider devuelve una estructura uniforme:
  {
    "provider": "registro_civil",
    "ok": True/False,
    "status": "matched|mismatch|partial|not_found|error|not_configured",
    "found": True/False,
    "payload": { ...datos oficiales },
    "checked_at": ISO,
    "source": "mock|api",
    "error": Optional[str],
  }

El validator (evidence_validator.py) llama estos providers y consolida.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------
# Mock provider · pseudo-determinístico
# --------------------------------------------------------------------
def _mock_match(seed: str) -> int:
    """Devuelve 0..99 estable por seed (id_value)."""
    h = hashlib.sha256(seed.encode("utf-8", errors="ignore")).digest()
    return h[0]  # 0..255 → tomamos los primeros bits


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def validate_with_registro_civil_mock(id_value: str, name: str) -> dict:
    """Mock del Registro Civil de Colombia.

    Determinístico:
      - id_value que termina en dígito par → match.
      - termina en 1, 3 → mismatch en nombre.
      - termina en 5 → not_found.
      - termina en 7, 9 → match parcial (nombre similar pero no exacto).
    """
    digit = (id_value or "0")[-1]
    base = {
        "provider": "registro_civil",
        "checked_at": _now_iso(),
        "source": "mock",
        "found": True,
    }
    if digit in ("0", "2", "4", "6", "8"):
        return {**base, "ok": True, "status": "matched",
                "payload": {
                    "cedula": id_value,
                    "nombre_completo": name or _generate_mock_name(id_value),
                    "estado": "vigente",
                    "fecha_expedicion": "2010-05-12",
                    "lugar_expedicion": "BOGOTÁ D.C.",
                }}
    if digit == "5":
        return {**base, "ok": True, "status": "not_found", "found": False, "payload": {}}
    if digit in ("1", "3"):
        return {**base, "ok": True, "status": "mismatch",
                "payload": {
                    "cedula": id_value,
                    "nombre_completo_oficial": _generate_mock_name(id_value),
                    "nombre_proporcionado": name,
                    "estado": "vigente",
                }}
    # 7, 9 → partial
    return {**base, "ok": True, "status": "partial",
            "payload": {
                "cedula": id_value,
                "nombre_completo_oficial": (name or "").upper() + " (variante con tilde)",
                "nombre_proporcionado": name,
                "estado": "vigente",
                "match_confidence": 0.78,
            }}


async def validate_with_rue_mock(nit: str, razon_social: str) -> dict:
    """Mock del Registro Único Empresarial."""
    digit = (nit or "0")[-1]
    base = {"provider": "rue", "checked_at": _now_iso(), "source": "mock", "found": True}
    if digit in ("0", "2", "4", "6", "8"):
        return {**base, "ok": True, "status": "matched",
                "payload": {
                    "nit": nit,
                    "razon_social": razon_social or f"EMPRESA DEMO {nit} SAS",
                    "tipo_societario": "S.A.S.",
                    "estado": "activa",
                    "fecha_constitucion": "2018-03-22",
                    "camara_comercio": "BOGOTÁ",
                    "matricula_mercantil": f"M-{nit[-6:]}",
                }}
    if digit == "5":
        return {**base, "ok": True, "status": "not_found", "found": False, "payload": {}}
    if digit in ("1", "3"):
        return {**base, "ok": True, "status": "mismatch",
                "payload": {
                    "nit": nit,
                    "razon_social_oficial": f"COMPAÑÍA {nit[-4:]} LTDA",
                    "razon_social_proporcionada": razon_social,
                    "estado": "activa",
                }}
    return {**base, "ok": True, "status": "partial",
            "payload": {
                "nit": nit,
                "razon_social_oficial": (razon_social or "").upper() + " S.A.S.",
                "razon_social_proporcionada": razon_social,
                "estado": "activa",
                "match_confidence": 0.82,
            }}


async def validate_with_rut_mock(tax_id: str) -> dict:
    """Mock del RUT (Registro Único Tributario · DIAN)."""
    digit = (tax_id or "0")[-1]
    base = {"provider": "rut", "checked_at": _now_iso(), "source": "mock", "found": True}
    if digit in ("0", "2", "4", "6", "8"):
        return {**base, "ok": True, "status": "matched",
                "payload": {
                    "nit_o_cedula": tax_id,
                    "responsabilidad_tributaria": ["O-13 Gran contribuyente" if int(digit) > 6 else "O-15 Responsable IVA"],
                    "estado": "activo",
                    "regimen": "Común" if int(digit) > 4 else "Simplificado",
                }}
    if digit == "5":
        return {**base, "ok": True, "status": "not_found", "found": False, "payload": {}}
    return {**base, "ok": True, "status": "partial",
            "payload": {
                "nit_o_cedula": tax_id,
                "estado": "activo · datos parciales",
                "match_confidence": 0.65,
            }}


def _generate_mock_name(id_value: str) -> str:
    """Genera nombre determinístico para el mock."""
    h = hashlib.sha256(id_value.encode()).hexdigest()
    nombres = ["María", "Carlos", "Andrea", "Juan", "Patricia", "Roberto", "Camila", "Diego"]
    apellidos1 = ["González", "Rodríguez", "López", "Hernández", "Pérez", "Sánchez", "Ramírez", "Torres"]
    apellidos2 = ["Martínez", "García", "Méndez", "Rojas", "Castro", "Vargas", "Restrepo", "Salazar"]
    n = int(h[:2], 16) % len(nombres)
    a1 = int(h[2:4], 16) % len(apellidos1)
    a2 = int(h[4:6], 16) % len(apellidos2)
    return f"{nombres[n]} {apellidos1[a1]} {apellidos2[a2]}"


# --------------------------------------------------------------------
# HttpProvider (placeholder · solo activado si env vars configuradas)
# --------------------------------------------------------------------
async def validate_with_registro_civil_http(id_value: str, name: str) -> dict:
    base_url = os.getenv("REGISTRO_CIVIL_API_BASE")
    api_key = os.getenv("REGISTRO_CIVIL_API_KEY")
    if not (base_url and api_key):
        return _not_configured("registro_civil")
    # En producción: httpx call al servicio.
    # Aquí dejamos placeholder · si llega aquí significa que las vars están
    # configuradas pero la implementación real debe hacerse cuando se firme
    # el contrato con la fuente oficial.
    return _not_configured("registro_civil", note="HTTP implementation pending")


async def validate_with_rue_http(nit: str, razon_social: str) -> dict:
    base_url = os.getenv("RUE_API_BASE")
    api_key = os.getenv("RUE_API_KEY")
    if not (base_url and api_key):
        return _not_configured("rue")
    return _not_configured("rue", note="HTTP implementation pending")


async def validate_with_rut_http(tax_id: str) -> dict:
    base_url = os.getenv("RUT_API_BASE")
    api_key = os.getenv("RUT_API_KEY")
    if not (base_url and api_key):
        return _not_configured("rut")
    return _not_configured("rut", note="HTTP implementation pending")


def _not_configured(provider: str, note: Optional[str] = None) -> dict:
    return {
        "provider": provider,
        "ok": False,
        "status": "not_configured",
        "found": False,
        "payload": {},
        "checked_at": _now_iso(),
        "source": "api",
        "error": note or f"{provider} no configurado (faltan env vars)",
    }


# --------------------------------------------------------------------
# Dispatchers · escoge entre Mock y Http
# --------------------------------------------------------------------
def _prefer_real() -> bool:
    """Si está env LEXAI_IDENTITY_FORCE_MOCK=1, ignora HTTP y usa Mock."""
    return os.getenv("LEXAI_IDENTITY_FORCE_MOCK") != "1"


async def validate_registro_civil(id_value: str, name: str) -> dict:
    """Intenta HTTP primero (si configurado), cae a Mock."""
    id_value = (id_value or "").strip()
    if not id_value:
        return _bad_input("registro_civil", "id_value vacío")
    if _prefer_real():
        result = await validate_with_registro_civil_http(id_value, name)
        if result.get("status") != "not_configured":
            return result
    return await validate_with_registro_civil_mock(id_value, name)


async def validate_rue(nit: str, razon_social: str) -> dict:
    nit = (nit or "").strip()
    if not nit:
        return _bad_input("rue", "nit vacío")
    if _prefer_real():
        result = await validate_with_rue_http(nit, razon_social)
        if result.get("status") != "not_configured":
            return result
    return await validate_with_rue_mock(nit, razon_social)


async def validate_rut(tax_id: str) -> dict:
    tax_id = (tax_id or "").strip()
    if not tax_id:
        return _bad_input("rut", "tax_id vacío")
    if _prefer_real():
        result = await validate_with_rut_http(tax_id)
        if result.get("status") != "not_configured":
            return result
    return await validate_with_rut_mock(tax_id)


def _bad_input(provider: str, msg: str) -> dict:
    return {
        "provider": provider,
        "ok": False,
        "status": "error",
        "found": False,
        "payload": {},
        "checked_at": _now_iso(),
        "source": "mock",
        "error": msg,
    }


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def normalize_id(value: str) -> str:
    """Normaliza una cédula/NIT: quita puntos, guiones, espacios."""
    return re.sub(r"[^0-9A-Za-z]", "", value or "")


def normalize_name(name: str) -> str:
    """Normaliza un nombre: trim, uppercase, single-space."""
    if not name:
        return ""
    return re.sub(r"\s+", " ", name.strip().upper())


def name_similarity(a: str, b: str) -> float:
    """0..1 · ratio de similitud aproximado entre 2 nombres normalizados.

    Implementación simple: tokens compartidos / max(tokens).
    Para producción se podría usar fuzz.ratio de rapidfuzz.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return round(inter / max(len(ta), len(tb)), 3)
