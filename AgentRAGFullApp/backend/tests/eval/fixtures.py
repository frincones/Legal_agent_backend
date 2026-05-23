"""Fixtures de casos para eval suite."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EvalCase:
    case_id: str
    doc_type: str
    intent: str
    brief: str
    expected_citations: list[str]
    expected_min_total_blocks: int
    expected_qa_min_score: float = 7.0


CASES_LABORAL = [
    EvalCase(
        case_id="lab-001-maria-perez",
        doc_type="demanda_laboral_ordinaria",
        intent="Redacta demanda ordinaria laboral por despido sin justa causa",
        brief="""Demandante: María Pérez González, C.C. 1.234.567.890.
Demandada: Comercializadora XYZ SAS NIT 900.123.456-7.
Salario $2.500.000. Ingreso 15-mar-2019, despido 30-abr-2026.
Cargo: Asistente Administrativa.""",
        expected_citations=["Art. 64", "Art. 65", "CST", "Ley 789"],
        expected_min_total_blocks=80,
    ),
    EvalCase(
        case_id="lab-002-contrato-realidad",
        doc_type="demanda_laboral_ordinaria",
        intent="Demanda por contrato realidad y reclamación de prestaciones",
        brief="""Demandante: Pedro Ramos, C.C. 80.000.000.
Demandada: Servicios Generales SA NIT 800.111.222-3.
Trabajó como técnico bajo contrato de prestación de servicios desde 01-ene-2022 a 31-dic-2025.
Honorarios mensuales $3.500.000. Cumplía horario fijo, recibía órdenes directas del supervisor.""",
        expected_citations=["Art. 22", "Art. 23", "Art. 24", "CST"],
        expected_min_total_blocks=70,
    ),
]


CASES_TUTELA = [
    EvalCase(
        case_id="tut-001-salud",
        doc_type="tutela",
        intent="Acción de tutela por derecho fundamental a la salud",
        brief="""Accionante: Juan López, C.C. 79.500.000.
Accionada: EPS Sura.
EPS niega autorización de cirugía cardíaca prescrita por especialista hace 60 días.
Pone en riesgo la vida del accionante.""",
        expected_citations=["Art. 49", "T-760", "Decreto 2591"],
        expected_min_total_blocks=40,
    ),
]


CASES_CONTRATO = [
    EvalCase(
        case_id="con-001-arriendo",
        doc_type="contrato_arrendamiento",
        intent="Contrato de arrendamiento de vivienda urbana",
        brief="""Arrendador: Inversiones Bogotá SAS NIT 900.001.001-1.
Arrendatario: Camila Suárez C.C. 1.020.345.678.
Inmueble: Carrera 15 # 88-22 Apto 501, Bogotá.
Canon mensual $1.800.000. Duración 12 meses.""",
        expected_citations=["Ley 820"],
        expected_min_total_blocks=40,
    ),
]


ALL_CASES = CASES_LABORAL + CASES_TUTELA + CASES_CONTRATO
