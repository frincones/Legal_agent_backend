"""
Modelos Pydantic para normas, derogaciones y jurisprudencia.
"""
from datetime import date, datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class TipoNorma(str, Enum):
    LEY = "LEY"
    DECRETO = "DECRETO"
    RESOLUCION = "RESOLUCION"
    CODIGO = "CODIGO"
    CIRCULAR = "CIRCULAR"
    ACUERDO = "ACUERDO"
    CONCEPTO = "CONCEPTO"


class EstadoNorma(str, Enum):
    VIGENTE = "VIGENTE"
    DEROGADA = "DEROGADA"
    MODIFICADA = "MODIFICADA"
    SUSPENDIDA = "SUSPENDIDA"


class TipoDerogacion(str, Enum):
    DEROGA_TOTAL = "DEROGA_TOTAL"
    DEROGA_PARCIAL = "DEROGA_PARCIAL"
    MODIFICA = "MODIFICA"
    ADICIONA = "ADICIONA"
    REGLAMENTA = "REGLAMENTA"
    SUSTITUYE = "SUSTITUYE"


class FuenteLegal(str, Enum):
    SENADO = "senado"
    FUNCION_PUBLICA = "funcion_publica"
    DATOS_GOV = "datos_gov"
    CORTE_CC = "corte_cc"
    CORTE_SUPREMA = "corte_suprema"
    DIARIO_OFICIAL = "diario_oficial"
    MANUAL = "manual"


class Corte(str, Enum):
    CORTE_CONSTITUCIONAL = "CORTE_CONSTITUCIONAL"
    CORTE_SUPREMA = "CORTE_SUPREMA"
    CONSEJO_ESTADO = "CONSEJO_ESTADO"


class TipoSentencia(str, Enum):
    TUTELA = "T"
    CONSTITUCIONALIDAD = "C"
    UNIFICACION = "SU"
    CASACION = "CASACION"
    AUTO = "AUTO"


# --- Create / Input Models ---

class NormaCreate(BaseModel):
    tipo: TipoNorma
    numero: Optional[int] = None
    anio: Optional[int] = None
    titulo: Optional[str] = None
    fecha_expedicion: Optional[date] = None
    fecha_vigencia: Optional[date] = None
    estado: EstadoNorma = EstadoNorma.VIGENTE
    entidad_emisora: Optional[str] = None
    sector: Optional[str] = None
    fuente: FuenteLegal = FuenteLegal.MANUAL
    fuente_url: Optional[str] = None
    fuente_id: Optional[str] = None
    texto_completo: Optional[str] = None
    resumen: Optional[str] = None
    temas: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class DerogacionCreate(BaseModel):
    norma_origen_id: str
    norma_destino_id: str
    tipo: TipoDerogacion
    articulos_afectados: list[str] = Field(default_factory=list)
    fecha_efecto: Optional[date] = None
    fuente_texto: Optional[str] = None
    detectado_por: str = "manual"
    confianza: float = 1.0


class JurisprudenciaCreate(BaseModel):
    corte: Corte
    sala: Optional[str] = None
    tipo_sentencia: Optional[TipoSentencia] = None
    numero: Optional[str] = None
    expediente: Optional[str] = None
    fecha: Optional[date] = None
    magistrado: Optional[str] = None
    temas: list[str] = Field(default_factory=list)
    es_precedente: bool = False
    normas_interpretadas: list[str] = Field(default_factory=list)
    ratio_decidendi: Optional[str] = None
    obiter_dicta: Optional[str] = None
    decision: Optional[str] = None
    fuente_url: Optional[str] = None
    fuente: FuenteLegal = FuenteLegal.MANUAL
    texto_completo: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


# --- Response / Output Models ---

class NormaResponse(BaseModel):
    id: str
    tipo: str
    numero: Optional[int] = None
    anio: Optional[int] = None
    titulo: Optional[str] = None
    estado: str
    fecha_expedicion: Optional[date] = None
    fuente_url: Optional[str] = None
    sector: Optional[str] = None
    temas: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class VigenciaResult(BaseModel):
    norma_id: Optional[str] = None
    tipo: str
    numero: Optional[int] = None
    anio: Optional[int] = None
    titulo: Optional[str] = None
    estado: str
    encontrada: bool = True
    derogaciones: list[dict] = Field(default_factory=list)
    cadena_completa: list[dict] = Field(default_factory=list)


class DerogacionDetectada(BaseModel):
    """Resultado de la detección automática de derogaciones en texto."""
    tipo_derogacion: TipoDerogacion
    norma_afectada_tipo: Optional[str] = None
    norma_afectada_numero: Optional[int] = None
    norma_afectada_anio: Optional[int] = None
    articulos_afectados: list[str] = Field(default_factory=list)
    texto_fuente: str
    confianza: float = 0.8


class LegalSearchResult(BaseModel):
    """Resultado unificado de búsqueda legal."""
    result_id: str
    content: str
    source_type: str  # document, norma, jurisprudencia, live_source
    source_name: str
    similarity: float
    estado: Optional[str] = None
    fuente_url: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class LiveSourceResult(BaseModel):
    """Resultado de búsqueda en fuente viva (internet)."""
    source: str  # datos_gov, senado, funcion_publica, etc.
    tipo: Optional[str] = None
    numero: Optional[int] = None
    anio: Optional[int] = None
    titulo: Optional[str] = None
    estado: Optional[str] = None
    url: Optional[str] = None
    preview: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
