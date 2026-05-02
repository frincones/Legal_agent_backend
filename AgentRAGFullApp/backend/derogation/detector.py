"""
Detector automático de derogaciones en textos legales colombianos.
Usa patrones regex para identificar artículos que derogan, modifican o adicionan otras normas.
"""
import re
import logging
from typing import Optional
from .models import DerogacionDetectada, TipoDerogacion

logger = logging.getLogger(__name__)

# Patrones regex para detectar derogaciones en texto legal colombiano
DEROGATION_PATTERNS = [
    # Derogación total: "Deróguese la Resolución 652 de 2012"
    {
        "pattern": r"[Dd]er[oó]g(?:a|u)(?:e|en)se?\s+(?:la|el|los|las)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|Acuerdo|Circular))\s+(?P<numero>\d+)\s+de\s+(?P<anio>\d{4})",
        "tipo": TipoDerogacion.DEROGA_TOTAL,
        "confianza": 0.95,
    },
    # Derogación con "quedan derogados"
    {
        "pattern": r"[Qq]ueda(?:n)?\s+derogad[oa]s?\s+(?:la|el|los|las)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|Acuerdo|Circular)(?:es)?)\s+(?P<numero>\d+)(?:\s+y\s+(?P<numero2>\d+))?\s+de\s+(?P<anio>\d{4})",
        "tipo": TipoDerogacion.DEROGA_TOTAL,
        "confianza": 0.95,
    },
    # "Deróguense las Resoluciones 652 y 1356 de 2012"
    {
        "pattern": r"[Dd]er[oó]g(?:a|u)(?:e|en)se?\s+(?:las|los)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|Acuerdo|Circular)(?:es)?)\s+(?P<numero>\d+)\s+y\s+(?P<numero2>\d+)\s+de\s+(?P<anio>\d{4})",
        "tipo": TipoDerogacion.DEROGA_TOTAL,
        "confianza": 0.95,
    },
    # "Se derogan las Resoluciones 652 y 1356 de 2012"
    {
        "pattern": r"[Ss]e\s+derogan\s+(?:las|los)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|Acuerdo|Circular)(?:es)?)\s+(?P<numero>\d+)\s+y\s+(?P<numero2>\d+)\s+de\s+(?P<anio>\d{4})",
        "tipo": TipoDerogacion.DEROGA_TOTAL,
        "confianza": 0.95,
    },
    # "Se deroga la Resolución 652 de 2012"
    {
        "pattern": r"[Ss]e\s+deroga(?:n)?\s+(?:la|el|las|los)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|Acuerdo|Circular)(?:es)?)\s+(?P<numero>\d+)\s+de\s+(?P<anio>\d{4})",
        "tipo": TipoDerogacion.DEROGA_TOTAL,
        "confianza": 0.95,
    },
    # Modificación: "Modifíquese el artículo X de la Ley Y de Z"
    {
        "pattern": r"[Mm]odif[ií]qu(?:e|en)se?\s+(?:el|los)\s+[Aa]rt[ií]culo(?:s)?\s+(?P<articulos>[\d,\sy]+)\s+de\s+(?:la|el)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|C[oó]digo\s+\w+(?:\s+\w+)*))\s+(?:(?P<numero>\d+)\s+de\s+)?(?P<anio>\d{4})?",
        "tipo": TipoDerogacion.MODIFICA,
        "confianza": 0.90,
    },
    # Sustitución: "Sustitúyase el artículo X"
    {
        "pattern": r"[Ss]ustit[uú]y(?:a|e)se?\s+(?:el|los)\s+[Aa]rt[ií]culo(?:s)?\s+(?P<articulos>[\d,\sy]+)\s+de\s+(?:la|el)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|C[oó]digo\s+\w+(?:\s+\w+)*))\s+(?:(?P<numero>\d+)\s+de\s+)?(?P<anio>\d{4})?",
        "tipo": TipoDerogacion.SUSTITUYE,
        "confianza": 0.90,
    },
    # Adición: "Adiciónese a la Ley X un artículo"
    {
        "pattern": r"[Aa]dici[oó]n(?:e|a)se?\s+(?:a\s+)?(?:la|el)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n|C[oó]digo\s+\w+(?:\s+\w+)*))\s+(?P<numero>\d+)\s+de\s+(?P<anio>\d{4})",
        "tipo": TipoDerogacion.ADICIONA,
        "confianza": 0.85,
    },
    # Reglamentación: "Por el cual se reglamenta la Ley X"
    {
        "pattern": r"[Rr]eglamenta(?:\s+parcialmente)?\s+(?:la|el)\s+(?P<tipo>(?:Ley|Decreto|Resoluci[oó]n))\s+(?P<numero>\d+)\s+de\s+(?P<anio>\d{4})",
        "tipo": TipoDerogacion.REGLAMENTA,
        "confianza": 0.85,
    },
    # Derogación implícita: "las disposiciones que le sean contrarias"
    {
        "pattern": r"[Dd]er[oó]g(?:a|u)(?:e|en)se?\s+(?:todas\s+)?(?:las\s+)?disposiciones\s+(?:que\s+le\s+sean\s+)?contrarias",
        "tipo": TipoDerogacion.DEROGA_PARCIAL,
        "confianza": 0.50,
    },
]

# Normalización de tipos de norma
TIPO_NORMALIZACION = {
    "ley": "LEY",
    "decreto": "DECRETO",
    "resolución": "RESOLUCION",
    "resolucion": "RESOLUCION",
    "acuerdo": "ACUERDO",
    "circular": "CIRCULAR",
    "código sustantivo del trabajo": "CODIGO",
    "codigo sustantivo del trabajo": "CODIGO",
    "código penal": "CODIGO",
    "codigo penal": "CODIGO",
    "código civil": "CODIGO",
    "codigo civil": "CODIGO",
}


def _normalizar_tipo(tipo_raw: str) -> Optional[str]:
    """Normaliza el tipo de norma a formato estándar."""
    tipo_lower = tipo_raw.lower().strip()
    for key, value in TIPO_NORMALIZACION.items():
        if key in tipo_lower:
            return value
    return tipo_lower.upper()


def _extraer_articulos(articulos_raw: str) -> list[str]:
    """Extrae lista de artículos de un string como '1, 2, 3 y 4'."""
    if not articulos_raw:
        return []
    # Reemplazar "y" por coma y limpiar
    cleaned = articulos_raw.replace(" y ", ",").replace("y", ",")
    arts = [a.strip() for a in cleaned.split(",") if a.strip().isdigit()]
    return [f"Art. {a}" for a in arts]


def _normalize_text(text: str) -> str:
    """Normalize unicode characters common in scraped legal HTML."""
    import unicodedata
    # Normalize to NFC (composed form) to handle combining characters
    text = unicodedata.normalize("NFC", text)
    # Replace common HTML artifacts
    replacements = {
        "\u00a0": " ",   # non-breaking space
        "\u2013": "-",   # en dash
        "\u2014": "-",   # em dash
        "\u201c": '"',   # left double quote
        "\u201d": '"',   # right double quote
        "\ufffd": "",    # replacement character
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def detect_derogations(text: str) -> list[DerogacionDetectada]:
    """
    Detecta derogaciones, modificaciones y adiciones en un texto legal.

    Args:
        text: Texto completo de la norma legal

    Returns:
        Lista de derogaciones detectadas con su tipo y confianza
    """
    if not text or len(text) < 50:
        return []

    text = _normalize_text(text)

    resultados = []
    seen = set()

    for pattern_def in DEROGATION_PATTERNS:
        pattern = pattern_def["pattern"]
        tipo_derogacion = pattern_def["tipo"]
        confianza_base = pattern_def["confianza"]

        for match in re.finditer(pattern, text):
            groups = match.groupdict()

            tipo_norma = _normalizar_tipo(groups.get("tipo", ""))
            numero = groups.get("numero")
            anio = groups.get("anio")
            articulos_raw = groups.get("articulos", "")

            # Deduplicar
            key = f"{tipo_derogacion}:{tipo_norma}:{numero}:{anio}"
            if key in seen:
                continue
            seen.add(key)

            det = DerogacionDetectada(
                tipo_derogacion=tipo_derogacion,
                norma_afectada_tipo=tipo_norma if tipo_norma else None,
                norma_afectada_numero=int(numero) if numero and numero.isdigit() else None,
                norma_afectada_anio=int(anio) if anio and anio.isdigit() else None,
                articulos_afectados=_extraer_articulos(articulos_raw),
                texto_fuente=match.group(0)[:300],
                confianza=confianza_base,
            )
            resultados.append(det)

            # Manejar "numero2" para patrones con "y" (ej: Resoluciones 652 y 1356)
            numero2 = groups.get("numero2")
            if numero2 and numero2.isdigit():
                key2 = f"{tipo_derogacion}:{tipo_norma}:{numero2}:{anio}"
                if key2 not in seen:
                    seen.add(key2)
                    det2 = DerogacionDetectada(
                        tipo_derogacion=tipo_derogacion,
                        norma_afectada_tipo=tipo_norma if tipo_norma else None,
                        norma_afectada_numero=int(numero2),
                        norma_afectada_anio=int(anio) if anio and anio.isdigit() else None,
                        articulos_afectados=[],
                        texto_fuente=match.group(0)[:300],
                        confianza=confianza_base,
                    )
                    resultados.append(det2)

    logger.info(f"Detectadas {len(resultados)} derogaciones en texto de {len(text)} chars")
    return resultados


def detect_derogations_in_articles(articles: list[dict]) -> list[DerogacionDetectada]:
    """
    Detecta derogaciones en una lista de artículos parseados.
    Prioriza artículos transitorios y de vigencia (típicamente los últimos).

    Args:
        articles: Lista de dicts con keys 'numero', 'titulo', 'contenido'
    """
    all_derogations = []

    # Priorizar artículos finales (transitorios, vigencia, derogatorios)
    priority_keywords = ["transitorio", "vigencia", "derogatorio", "derogatoria", "derogación"]

    for article in articles:
        contenido = article.get("contenido", "")
        titulo = article.get("titulo", "").lower()

        # Buscar en todos los artículos pero dar más confianza a transitorios
        derogations = detect_derogations(contenido)

        is_priority = any(kw in titulo for kw in priority_keywords)
        if is_priority:
            for d in derogations:
                d.confianza = min(d.confianza + 0.05, 1.0)

        all_derogations.extend(derogations)

    return all_derogations
