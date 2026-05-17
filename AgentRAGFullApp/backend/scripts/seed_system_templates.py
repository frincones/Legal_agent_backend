"""Seed initial system templates · firm_id IS NULL · materia coverage.

Inserts ~10 curated system templates into `user_templates` (system catalog).
Idempotent · uses ON CONFLICT (id) DO NOTHING + dedup by content hash.

Run from backend root:

    python -m scripts.seed_system_templates --dry-run
    python -m scripts.seed_system_templates --apply

Requires the migration 2026_05_24_templates_specialization.sql to be applied
first (firm_id must be nullable + new columns must exist).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SystemTemplate:
    """A curated template inserted into user_templates with firm_id=NULL."""
    name: str
    doc_type: str
    materia: str
    subtype: Optional[str]
    content_md: str
    applicable_norms: list[str]
    quality_score: float
    clauses: dict


# ──────────────────────────────────────────────────────────────
# Seed library · 10 curated templates · expand via curator UI.
# Quality scores reflect "curated minimum baseline" · 0.85+.
# ──────────────────────────────────────────────────────────────
SEED_TEMPLATES: list[SystemTemplate] = [
    # ── Laboral ──────────────────────────────────────────────
    SystemTemplate(
        name="Demanda laboral · despido injustificado",
        doc_type="demanda_laboral",
        materia="laboral",
        subtype="demanda_laboral_despido_injustificado",
        applicable_norms=["CST art. 64", "CST art. 65", "Ley 1564/2012 art. 25"],
        quality_score=0.88,
        clauses={
            "sections": [
                {"id": "encabezado", "title": "Encabezado", "required": True},
                {"id": "partes", "title": "Identificación de las partes", "required": True},
                {"id": "cuantia", "title": "Cuantía", "required": True},
                {"id": "hechos", "title": "Hechos", "required": True, "min_items": 5},
                {"id": "pretensiones", "title": "Pretensiones", "required": True},
                {"id": "fundamentos", "title": "Fundamentos de derecho", "required": True},
                {"id": "pruebas", "title": "Pruebas", "required": True},
                {"id": "anexos", "title": "Anexos", "required": True},
                {"id": "notificaciones", "title": "Notificaciones", "required": True},
            ]
        },
        content_md="""SEÑOR/A JUEZ LABORAL DEL CIRCUITO DE {{ciudad}} (REPARTO)

{{nombre_demandante}}, mayor de edad, identificado/a con cédula de
ciudadanía No. {{cc_demandante}} de {{ciudad}}, por intermedio de
apoderado/a debidamente constituido/a, formula DEMANDA ORDINARIA
LABORAL contra {{nombre_demandado}}, sociedad identificada con
NIT {{nit_demandado}}, representada legalmente por {{representante_legal}},
con base en los siguientes hechos y pretensiones:

## I. PARTES

**Demandante:** {{nombre_demandante}} · C.C. {{cc_demandante}} ·
domicilio {{direccion_demandante}}.

**Demandado:** {{nombre_demandado}} · NIT {{nit_demandado}} ·
domicilio {{direccion_demandado}}.

## II. CUANTÍA

La cuantía de la presente acción se estima en {{cuantia_cop}} pesos
moneda legal ({{cuantia_smmlv}} SMMLV de {{anio_smmlv}}), conforme
al artículo 25 del Código General del Proceso.

## III. HECHOS

1. El/la demandante suscribió contrato de trabajo a término
   {{tipo_contrato}} con {{nombre_demandado}} el día
   {{fecha_ingreso}}.

2. El cargo desempeñado fue {{cargo}}, devengando un salario mensual
   de {{salario_mensual_cop}}.

3. El día {{fecha_terminacion}}, el empleador dio por terminado el
   contrato de trabajo de manera unilateral y sin justa causa,
   mediante {{forma_terminacion}}.

4. {{hechos_adicionales}}

## IV. PRETENSIONES

1. Que se declare que entre las partes existió un contrato de trabajo
   a término {{tipo_contrato}} desde el {{fecha_ingreso}} hasta el
   {{fecha_terminacion}}.

2. Que se declare que el demandado terminó unilateralmente el contrato
   sin justa causa, conforme al artículo 64 del Código Sustantivo del
   Trabajo.

3. Que como consecuencia se condene a {{nombre_demandado}} a pagar al
   demandante:
   a) Indemnización por despido injustificado conforme al art. 64 CST.
   b) Salarios moratorios conforme al art. 65 CST hasta el pago efectivo.
   c) Prestaciones sociales adeudadas (cesantías, intereses, primas,
      vacaciones).
   d) Indexación e intereses legales.
   e) Costas y agencias en derecho.

## V. FUNDAMENTOS DE DERECHO

Invoco como fundamento jurídico los artículos 22, 23, 64 y 65 del
Código Sustantivo del Trabajo, así como la jurisprudencia consolidada
de la Sala Laboral de la Corte Suprema de Justicia en materia de
despido sin justa causa y sanción por mora.

## VI. PRUEBAS

- Contrato de trabajo (ANEXO 1).
- Comprobantes de pago (ANEXO 2).
- Carta de terminación / comunicación del empleador (ANEXO 3).
- Declaraciones extraprocesales de testigos (ANEXO 4).

## VII. ANEXOS

Se acompañan los documentos relacionados en la sección de pruebas,
debidamente rotulados en mayúscula sostenida.

## VIII. NOTIFICACIONES

**Demandante:** {{correo_demandante}} · {{direccion_demandante}}.
**Demandado:** {{correo_demandado}} · {{direccion_demandado}}.

Atentamente,

[FIRMA]
{{nombre_apoderado}}
C.C. {{cc_apoderado}}
T.P. {{tp_apoderado}} del C.S. de la J.
Correo: {{correo_apoderado}}
""",
    ),
    # ── Laboral · contrato ───────────────────────────────────
    SystemTemplate(
        name="Contrato de trabajo · término indefinido",
        doc_type="contrato",
        materia="laboral",
        subtype="contrato_trabajo_termino_indefinido",
        applicable_norms=["CST art. 22", "CST art. 47", "Ley 50/1990"],
        quality_score=0.86,
        clauses={
            "sections": [
                {"id": "partes", "title": "Partes", "required": True},
                {"id": "objeto", "title": "Objeto", "required": True},
                {"id": "remuneracion", "title": "Remuneración", "required": True},
                {"id": "jornada", "title": "Jornada", "required": True},
                {"id": "vigencia", "title": "Vigencia", "required": True},
                {"id": "obligaciones", "title": "Obligaciones", "required": True},
                {"id": "terminacion", "title": "Terminación", "required": True},
            ]
        },
        content_md="""CONTRATO INDIVIDUAL DE TRABAJO A TÉRMINO INDEFINIDO

Entre los suscritos, a saber:

**EL EMPLEADOR:** {{nombre_empleador}}, identificado con
NIT {{nit_empleador}}, representado legalmente por
{{representante_legal_empleador}}, en adelante EL EMPLEADOR.

**EL TRABAJADOR:** {{nombre_trabajador}}, mayor de edad,
identificado con C.C. {{cc_trabajador}}, en adelante EL TRABAJADOR.

Hemos convenido celebrar el presente contrato individual de trabajo a
término indefinido conforme al Código Sustantivo del Trabajo y las
siguientes cláusulas:

**PRIMERA · OBJETO.** EL TRABAJADOR se obliga a prestar sus servicios
personales en el cargo de {{cargo}} con las funciones inherentes a
dicho cargo.

**SEGUNDA · REMUNERACIÓN.** EL EMPLEADOR pagará una remuneración
mensual de {{salario_mensual_cop}} pesos, más {{auxilio_transporte}}
de auxilio de transporte cuando aplique.

**TERCERA · JORNADA.** La jornada será de {{jornada_horas}} horas
semanales distribuidas según la programación del EMPLEADOR.

**CUARTA · VIGENCIA.** Este contrato inicia el {{fecha_ingreso}} y se
celebra a término indefinido.

**QUINTA · OBLIGACIONES.** Las partes se obligan a cumplir las
disposiciones del Código Sustantivo del Trabajo y el Reglamento
Interno del EMPLEADOR.

**SEXTA · TERMINACIÓN.** Cualquiera de las partes podrá dar por
terminado el contrato conforme a los artículos 61 a 65 del CST.

Para constancia, se firma en {{ciudad}}, el {{fecha_contrato}}.

EL EMPLEADOR                                EL TRABAJADOR

___________________                         ___________________
{{representante_legal_empleador}}           {{nombre_trabajador}}
NIT {{nit_empleador}}                       C.C. {{cc_trabajador}}
""",
    ),
    # ── Tutela · salud ───────────────────────────────────────
    SystemTemplate(
        name="Acción de tutela · derecho a la salud",
        doc_type="tutela",
        materia="constitucional",
        subtype="tutela_salud_eps",
        applicable_norms=["Const. art. 86", "Decreto 2591/1991", "Ley 100/1993 art. 153"],
        quality_score=0.90,
        clauses={
            "sections": [
                {"id": "encabezado", "title": "Encabezado", "required": True},
                {"id": "partes", "title": "Partes", "required": True},
                {"id": "derechos", "title": "Derechos invocados", "required": True},
                {"id": "hechos", "title": "Hechos", "required": True, "min_items": 3},
                {"id": "pretensiones", "title": "Pretensiones", "required": True},
                {"id": "juramento", "title": "Juramento", "required": True},
                {"id": "pruebas", "title": "Pruebas", "required": True},
            ]
        },
        content_md="""SEÑOR/A JUEZ DE TUTELA DE {{ciudad}} (REPARTO)

{{nombre_accionante}}, mayor de edad, identificado/a con C.C.
{{cc_accionante}}, actuando en nombre propio, presento ACCIÓN DE TUTELA
contra {{nombre_accionado}} (EPS), identificada con NIT {{nit_accionado}},
por la vulneración de mis derechos fundamentales a la salud y la vida
digna, con base en los siguientes:

## I. DERECHOS FUNDAMENTALES VULNERADOS

- Derecho fundamental a la **salud** (Const. art. 49 · Ley 1751/2015).
- Derecho fundamental a la **vida en condiciones dignas** (Const. art. 11).
- {{otros_derechos}}

## II. HECHOS

1. El/la suscrito/a es afiliado/a a la EPS {{nombre_accionado}} desde
   {{fecha_afiliacion}} en régimen {{regimen}}.

2. El día {{fecha_diagnostico}} fui diagnosticado/a con
   {{diagnostico}}, según consta en historia clínica
   adjunta (ANEXO 1).

3. El/la médico/a tratante ordenó {{prestacion_ordenada}}, según
   orden médica del {{fecha_orden_medica}} (ANEXO 2).

4. Pese a las solicitudes presentadas el {{fechas_solicitudes}}, la EPS
   {{nombre_accionado}} ha negado / demorado injustificadamente la
   autorización requerida.

5. {{hechos_adicionales}}

## III. PRETENSIONES

1. **PRIMERA.** Que se TUTELEN mis derechos fundamentales a la salud y
   la vida digna.

2. **SEGUNDA.** Que se ORDENE a la EPS {{nombre_accionado}} que en el
   término improrrogable de 48 horas autorice y suministre
   {{prestacion_ordenada}}.

3. **TERCERA.** Que se ORDENE el TRATAMIENTO INTEGRAL para la
   patología {{diagnostico}}, incluyendo todos los servicios,
   medicamentos, procedimientos e insumos requeridos.

## IV. JURAMENTO

Bajo la gravedad del juramento manifiesto que no he presentado otra
acción de tutela por los mismos hechos y derechos contra la misma
entidad (Decreto 2591/1991, art. 38).

## V. PRUEBAS

- Historia clínica (ANEXO 1).
- Orden médica (ANEXO 2).
- Solicitudes presentadas a la EPS (ANEXO 3).
- Cédula del accionante (ANEXO 4).

## VI. NOTIFICACIONES

**Accionante:** {{correo_accionante}} · {{direccion_accionante}} ·
{{telefono_accionante}}.

**Accionado:** Dirección oficial registrada en Cámara de Comercio /
SuperSalud · correo: {{correo_accionado}}.

Atentamente,

{{nombre_accionante}}
C.C. {{cc_accionante}}
""",
    ),
    # ── Civil · demanda ejecutiva ────────────────────────────
    SystemTemplate(
        name="Demanda ejecutiva singular · obligación dineraria",
        doc_type="demanda_civil",
        materia="civil",
        subtype="demanda_ejecutiva_singular",
        applicable_norms=["Ley 1564/2012 art. 422", "Ley 1564/2012 art. 430"],
        quality_score=0.87,
        clauses={
            "sections": [
                {"id": "encabezado", "title": "Encabezado", "required": True},
                {"id": "partes", "title": "Partes", "required": True},
                {"id": "titulo", "title": "Título ejecutivo", "required": True},
                {"id": "pretensiones", "title": "Pretensiones", "required": True},
                {"id": "medidas", "title": "Medidas cautelares", "required": True},
                {"id": "anexos", "title": "Anexos", "required": True},
            ]
        },
        content_md="""SEÑOR/A JUEZ CIVIL DEL CIRCUITO DE {{ciudad}} (REPARTO)

{{nombre_demandante}}, identificado/a con C.C. {{cc_demandante}},
por medio de apoderado/a, formulo DEMANDA EJECUTIVA SINGULAR contra
{{nombre_demandado}}, identificado/a con C.C./NIT {{id_demandado}},
con base en los siguientes elementos:

## I. PARTES

**Demandante:** {{nombre_demandante}} · {{direccion_demandante}}.
**Demandado:** {{nombre_demandado}} · {{direccion_demandado}}.

## II. TÍTULO EJECUTIVO

El presente proceso se funda en el siguiente título ejecutivo:
{{descripcion_titulo}}, suscrito el {{fecha_titulo}} por
{{nombre_demandado}}, por valor de {{valor_obligacion_cop}}.

Dicho título reúne los requisitos del artículo 422 del CGP, en cuanto
contiene una obligación clara, expresa y exigible, está vencido y
proviene del deudor.

## III. PRETENSIONES

**PRIMERA.** Librar MANDAMIENTO EJECUTIVO de pago a favor del
demandante y a cargo del demandado, por la suma de
{{valor_obligacion_cop}}, más intereses moratorios desde
{{fecha_vencimiento}} y costas procesales.

**SEGUNDA.** Decretar las medidas cautelares previas previstas en el
artículo 599 del CGP.

## IV. MEDIDAS CAUTELARES

Solicito decretar embargo y secuestro de los bienes muebles e
inmuebles del demandado en cuantía suficiente, previa constitución de
la caución correspondiente.

## V. ANEXOS

- Título ejecutivo original (ANEXO 1).
- Poder (ANEXO 2).
- Cédula del demandante (ANEXO 3).

## VI. NOTIFICACIONES

**Demandante:** {{correo_demandante}}.
**Demandado:** {{direccion_demandado}} · {{correo_demandado}}.

Atentamente,

{{nombre_apoderado}}
T.P. {{tp_apoderado}} del C.S. de la J.
""",
    ),
    # ── Derecho de petición ─────────────────────────────────
    SystemTemplate(
        name="Derecho de petición · información",
        doc_type="derecho_peticion",
        materia="administrativo",
        subtype="derecho_peticion_informacion",
        applicable_norms=["Const. art. 23", "Ley 1755/2015"],
        quality_score=0.85,
        clauses={
            "sections": [
                {"id": "encabezado", "title": "Encabezado", "required": True},
                {"id": "identificacion", "title": "Identificación", "required": True},
                {"id": "asunto", "title": "Asunto", "required": True},
                {"id": "peticion", "title": "Petición", "required": True},
                {"id": "fundamentos", "title": "Fundamentos", "required": True},
                {"id": "notificaciones", "title": "Notificaciones", "required": True},
            ]
        },
        content_md="""{{ciudad}}, {{fecha_actual}}

Señor/a
{{cargo_destinatario}}
{{entidad_destinatario}}
{{ciudad_destinatario}}

**Asunto:** Derecho de petición de información en ejercicio del
artículo 23 de la Constitución Política y la Ley 1755 de 2015.

Respetado/a {{cargo_destinatario}}:

{{nombre_peticionario}}, mayor de edad, identificado/a con C.C.
{{cc_peticionario}}, presento de manera respetuosa el presente
DERECHO DE PETICIÓN de información, en los siguientes términos:

## OBJETO DE LA PETICIÓN

Solicito a la entidad de la referencia que se sirva suministrar la
siguiente información:

1. {{informacion_solicitada_1}}
2. {{informacion_solicitada_2}}
3. {{informacion_solicitada_3}}

## FUNDAMENTOS

El artículo 23 de la Constitución Política reconoce el derecho
fundamental de petición, desarrollado por la Ley 1755 de 2015, la cual
establece que las peticiones de información deben ser resueltas en un
término máximo de diez (10) días hábiles.

## NOTIFICACIONES

Recibiré respuesta en la siguiente dirección:
{{direccion_peticionario}}, o al correo electrónico
{{correo_peticionario}}.

Atentamente,

{{nombre_peticionario}}
C.C. {{cc_peticionario}}
""",
    ),
    # ── Contrato civil de arrendamiento ──────────────────────
    SystemTemplate(
        name="Contrato de arrendamiento de vivienda urbana",
        doc_type="contrato",
        materia="civil",
        subtype="contrato_arrendamiento_vivienda",
        applicable_norms=["Ley 820/2003"],
        quality_score=0.86,
        clauses={
            "sections": [
                {"id": "partes", "title": "Partes", "required": True},
                {"id": "inmueble", "title": "Inmueble", "required": True},
                {"id": "canon", "title": "Canon y forma de pago", "required": True},
                {"id": "duracion", "title": "Duración", "required": True},
                {"id": "deposito", "title": "Depósito y garantías", "required": True},
                {"id": "obligaciones", "title": "Obligaciones", "required": True},
                {"id": "terminacion", "title": "Terminación", "required": True},
            ]
        },
        content_md="""CONTRATO DE ARRENDAMIENTO DE VIVIENDA URBANA

Entre los suscritos:

**ARRENDADOR:** {{nombre_arrendador}}, identificado con
C.C./NIT {{id_arrendador}}.

**ARRENDATARIO:** {{nombre_arrendatario}}, identificado con
C.C. {{cc_arrendatario}}.

Convenimos celebrar el presente contrato de arrendamiento de vivienda
urbana, conforme a la Ley 820 de 2003 y las siguientes cláusulas:

**PRIMERA · INMUEBLE.** El arrendador entrega en arrendamiento el
inmueble ubicado en {{direccion_inmueble}}, {{ciudad}}.

**SEGUNDA · CANON.** El canon mensual es de {{canon_cop}} pesos,
pagaderos los primeros cinco (5) días de cada mes.

**TERCERA · DURACIÓN.** El presente contrato tiene una duración de
{{duracion_meses}} meses contados desde el {{fecha_inicio}}.

**CUARTA · DEPÓSITO.** El arrendatario entrega como garantía un (1)
canon de arrendamiento, restituible al final del contrato previa
verificación del estado del inmueble.

**QUINTA · OBLIGACIONES.** Aplican las obligaciones recíprocas de la
Ley 820/2003.

**SEXTA · TERMINACIÓN.** Cualquier parte podrá dar por terminado el
contrato dando aviso con tres (3) meses de antelación, conforme al
artículo 22 de la Ley 820/2003.

Para constancia, se firma en {{ciudad}}, el {{fecha_contrato}}.

ARRENDADOR                                  ARRENDATARIO
___________________                         ___________________
{{nombre_arrendador}}                       {{nombre_arrendatario}}
""",
    ),
    # ── Recurso de reposición administrativo ────────────────
    SystemTemplate(
        name="Recurso de reposición · acto administrativo",
        doc_type="recurso_reposicion",
        materia="administrativo",
        subtype="recurso_reposicion_acto",
        applicable_norms=["Ley 1437/2011 art. 74", "Ley 1437/2011 art. 76"],
        quality_score=0.85,
        clauses={
            "sections": [
                {"id": "encabezado", "title": "Encabezado", "required": True},
                {"id": "identificacion", "title": "Identificación", "required": True},
                {"id": "acto", "title": "Acto recurrido", "required": True},
                {"id": "fundamentos", "title": "Fundamentos", "required": True},
                {"id": "peticion", "title": "Petición", "required": True},
            ]
        },
        content_md="""Señor/a
{{cargo_funcionario}}
{{entidad}}
{{ciudad}}

**Asunto:** Recurso de reposición contra {{acto_recurrido}}.

{{nombre_recurrente}}, identificado/a con C.C. {{cc_recurrente}},
dentro del término legal de diez (10) días siguientes a la
notificación del acto administrativo {{acto_recurrido}}, expedido el
{{fecha_acto}}, presento RECURSO DE REPOSICIÓN, con base en los
siguientes:

## ACTO RECURRIDO

{{descripcion_acto}}, notificado el día {{fecha_notificacion}}.

## FUNDAMENTOS

{{fundamentos_recurso}}

## PETICIÓN

PRIMERA. Revocar el acto administrativo {{acto_recurrido}} en su
integridad.

SUBSIDIARIA. Modificar el acto en el sentido de
{{modificacion_subsidiaria}}.

## NOTIFICACIONES

Recibiré notificaciones en {{direccion_recurrente}} ·
{{correo_recurrente}}.

Atentamente,

{{nombre_recurrente}}
C.C. {{cc_recurrente}}
""",
    ),
    # ── Demanda de alimentos ─────────────────────────────────
    SystemTemplate(
        name="Demanda de alimentos",
        doc_type="demanda_civil",
        materia="familiar",
        subtype="demanda_alimentos",
        applicable_norms=["Ley 1098/2006 art. 24", "CC art. 411"],
        quality_score=0.85,
        clauses={
            "sections": [
                {"id": "encabezado", "title": "Encabezado", "required": True},
                {"id": "partes", "title": "Partes", "required": True},
                {"id": "hechos", "title": "Hechos", "required": True, "min_items": 3},
                {"id": "pretensiones", "title": "Pretensiones", "required": True},
                {"id": "pruebas", "title": "Pruebas", "required": True},
            ]
        },
        content_md="""SEÑOR/A JUEZ DE FAMILIA DE {{ciudad}} (REPARTO)

{{nombre_alimentaria}}, en calidad de representante legal de su
hijo/a menor {{nombre_hijo}}, identificado/a con
{{id_hijo}}, presento DEMANDA DE ALIMENTOS contra
{{nombre_alimentante}}, identificado/a con C.C.
{{cc_alimentante}}, con base en los siguientes hechos:

## HECHOS

1. {{nombre_hijo}} es hijo/a de {{nombre_alimentante}} según
   {{prueba_filiacion}}.

2. El/la menor se encuentra bajo la guarda y custodia de la madre /
   padre demandante.

3. El/la demandado/a {{nombre_alimentante}} cuenta con capacidad
   económica para suministrar alimentos.

## PRETENSIONES

1. Que se fije cuota alimentaria mensual a favor del/la menor
   {{nombre_hijo}}, por la suma de {{cuota_cop}} pesos.

2. Que se ordene el pago de cuotas extraordinarias para gastos
   educativos, médicos y de vestuario.

## PRUEBAS

- Registro civil de nacimiento (ANEXO 1).
- Certificados laborales del demandado (ANEXO 2).
- Recibos de gastos del menor (ANEXO 3).

Atentamente,

{{nombre_alimentaria}}
""",
    ),
    # ── Constitución SAS ─────────────────────────────────────
    SystemTemplate(
        name="Documento de constitución de S.A.S.",
        doc_type="contrato",
        materia="mercantil",
        subtype="constitucion_sas",
        applicable_norms=["Ley 1258/2008"],
        quality_score=0.87,
        clauses={
            "sections": [
                {"id": "partes", "title": "Otorgantes", "required": True},
                {"id": "denominacion", "title": "Denominación social", "required": True},
                {"id": "objeto", "title": "Objeto social", "required": True},
                {"id": "capital", "title": "Capital", "required": True},
                {"id": "administracion", "title": "Administración", "required": True},
                {"id": "duracion", "title": "Duración", "required": True},
            ]
        },
        content_md="""DOCUMENTO PRIVADO DE CONSTITUCIÓN
SOCIEDAD POR ACCIONES SIMPLIFICADA (S.A.S.)

Entre los suscritos, {{accionistas_listado}}, mayores de edad,
identificados como aparecen al pie de sus firmas, hemos acordado
constituir una sociedad por acciones simplificada (S.A.S.) regida por
la Ley 1258 de 2008 y los siguientes estatutos:

**PRIMERA · DENOMINACIÓN.** La sociedad se denominará
{{razon_social}} S.A.S.

**SEGUNDA · DOMICILIO.** El domicilio principal será la ciudad de
{{ciudad}}.

**TERCERA · DURACIÓN.** La duración será {{duracion}} (puede ser
indefinida).

**CUARTA · OBJETO.** La sociedad tendrá por objeto principal
{{objeto_social}}.

**QUINTA · CAPITAL.** El capital autorizado es de {{capital_cop}}
pesos, dividido en {{numero_acciones}} acciones de valor nominal
{{valor_nominal}} cada una. El capital suscrito y pagado es
{{capital_suscrito_cop}}.

**SEXTA · ADMINISTRACIÓN.** La sociedad será administrada por un
representante legal designado por la asamblea de accionistas.

**SÉPTIMA · ASAMBLEA.** La asamblea se reunirá ordinariamente cada
año, dentro de los tres meses siguientes al cierre del ejercicio.

Para constancia, se firma en {{ciudad}}, el {{fecha_constitucion}}.

ACCIONISTAS

{{firmas_accionistas}}
""",
    ),
    # ── Hábeas data ──────────────────────────────────────────
    SystemTemplate(
        name="Solicitud de hábeas data · corrección de información",
        doc_type="derecho_peticion",
        materia="constitucional",
        subtype="habeas_data_correccion",
        applicable_norms=["Const. art. 15", "Ley 1581/2012", "Ley 1266/2008"],
        quality_score=0.84,
        clauses={
            "sections": [
                {"id": "encabezado", "title": "Encabezado", "required": True},
                {"id": "asunto", "title": "Asunto", "required": True},
                {"id": "hechos", "title": "Hechos", "required": True},
                {"id": "peticion", "title": "Petición", "required": True},
            ]
        },
        content_md="""{{ciudad}}, {{fecha_actual}}

Señor/a
{{cargo_destinatario}}
{{entidad_destinatario}}

**Asunto:** Solicitud de hábeas data · corrección / actualización /
supresión de información (Const. art. 15 · Ley 1581 de 2012).

{{nombre_titular}}, identificado/a con C.C. {{cc_titular}}, en
ejercicio del derecho fundamental al hábeas data, presento la
siguiente solicitud:

## HECHOS

{{descripcion_hechos}}

## PETICIÓN

Solicito respetuosamente que en el término de quince (15) días
hábiles se proceda a {{accion_solicitada}} la información personal
descrita, en cumplimiento del régimen de protección de datos
personales vigente.

## NOTIFICACIONES

Recibiré respuesta en {{direccion_titular}} · {{correo_titular}}.

Atentamente,

{{nombre_titular}}
C.C. {{cc_titular}}
""",
    ),
]


def template_hash(t: SystemTemplate) -> str:
    """Stable hash for dedup · same name + materia + content → same hash."""
    h = hashlib.sha256()
    h.update(t.name.encode("utf-8"))
    h.update(t.materia.encode("utf-8"))
    h.update(t.content_md.encode("utf-8"))
    return h.hexdigest()[:32]


async def apply_seeds(dry_run: bool = True) -> None:
    """Insert each SEED_TEMPLATES row into user_templates if not present."""
    from utils.db import get_storage
    import json

    storage = await get_storage()
    inserted = 0
    skipped = 0
    errors = 0

    async with storage.pool.acquire() as conn:
        for tpl in SEED_TEMPLATES:
            # Dedup by exact name + firm_id IS NULL (system catalog).
            existing = await conn.fetchval(
                """
                select id from user_templates
                 where firm_id is null
                   and name = $1
                 limit 1
                """,
                tpl.name,
            )
            if existing:
                skipped += 1
                logger.info("SKIP existing system template: %s", tpl.name)
                continue

            if dry_run:
                inserted += 1
                logger.info("DRY-RUN would insert: %s [materia=%s]", tpl.name, tpl.materia)
                continue

            try:
                # Extract variables {{xxx}} from content for the variables column.
                import re
                vars_found = sorted(set(re.findall(r"\{\{\s*([a-zA-Z][\w.]*)\s*\}\}", tpl.content_md)))

                await conn.execute(
                    """
                    insert into user_templates
                      (firm_id, owner_id, name, doc_type, jurisdiction,
                       materia, subtype, content_md, variables,
                       applicable_norms, quality_score, clauses_jsonb,
                       is_default_for_type, metadata)
                    values
                      (null, null, $1, $2, 'CO',
                       $3::materia_legal, $4, $5, $6::jsonb,
                       $7, $8, $9::jsonb,
                       false, $10::jsonb)
                    """,
                    tpl.name,
                    tpl.doc_type,
                    tpl.materia,
                    tpl.subtype,
                    tpl.content_md,
                    json.dumps(vars_found),
                    tpl.applicable_norms,
                    tpl.quality_score,
                    json.dumps(tpl.clauses),
                    json.dumps({"seed_source": "scripts.seed_system_templates", "version": 1}),
                )
                inserted += 1
                logger.info("INSERT system template: %s [materia=%s]", tpl.name, tpl.materia)
            except Exception as e:
                errors += 1
                logger.exception("FAILED to insert %s: %s", tpl.name, e)

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"[{mode}] inserted={inserted} skipped={skipped} errors={errors}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed system templates (firm_id IS NULL).")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Report only · no DB writes")
    g.add_argument("--apply", action="store_true", help="Actually insert rows")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        asyncio.run(apply_seeds(dry_run=args.dry_run))
    except Exception as e:
        logger.exception("seed failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
