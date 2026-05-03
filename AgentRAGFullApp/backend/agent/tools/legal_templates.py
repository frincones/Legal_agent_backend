"""Plantillas legales colombianas profesionales · LexAI v2.

Estas plantillas siguen la estructura formal estándar usada por despachos
colombianos: encabezamiento al juez, partes identificadas, hechos
numerados (romano y arábigo), pretensiones, fundamentos de derecho,
pruebas, anexos, notificaciones y firma con tarjeta profesional.

Fuentes y referencias para los formatos:
  · Rama Judicial — formatos oficiales: https://www.ramajudicial.gov.co
  · Decreto 2591/1991 (tutela) — Defensoría del Pueblo
  · CGP Ley 1564/2012 — estructura demanda y recurso apelación
  · CST Art. 145 + Ley 50/1990 — laboral
  · C.P. Art. 86 — tutela
  · CPACA Ley 1437/2011 — administrativo
  · Cámaras de Comercio — contratos comerciales
  · Decreto 1377/2013 — habeas data

Para extender: agregar nueva entrada al dict TEMPLATES con campos:
  - title:        nombre humano legible (mostrado al usuario)
  - description:  para qué sirve (1 línea)
  - applicable:   materia legal donde aplica
  - placeholders: lista de campos a rellenar (UI puede mostrar form)
  - markdown:     plantilla en markdown con {placeholder} a sustituir
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


# ─────────────────────────────────────────────────────────────────────
# Plantillas — markdown listo para Canvas
# Convenciones:
#   - {dato}  → substituible por facts del caller
#   - "[...]" → placeholder visible si no hay dato (el abogado lo edita)
#   - Estructura formal con headings H1/H2 que el editor renderiza bien.
# ─────────────────────────────────────────────────────────────────────


def _today_es() -> str:
    """Fecha en formato 'Bogotá D.C., 03 de mayo de 2026'."""
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    now = datetime.now()
    return f"Bogotá D.C., {now.day:02d} de {meses[now.month - 1]} de {now.year}"


# ════════════════════════════════════════════════════════════════════════
# 1 · DEMANDA ORDINARIA LABORAL · CST + CGP Art. 281+
# ════════════════════════════════════════════════════════════════════════

DEMANDA_ORDINARIA_LABORAL = """\
{fecha}

Señor[a]
JUEZ ([numero] LABORAL DEL CIRCUITO DE BOGOTÁ) (REPARTO)
E. S. D.

**REFERENCIA:** DEMANDA ORDINARIA LABORAL DE PRIMERA INSTANCIA
**DEMANDANTE:** {nombre_actor}, identificado(a) con C.C. No. {cedula_actor}
**DEMANDADO:** {nombre_demandado}, NIT {nit_demandado}
**CUANTÍA:** {cuantia} ({cuantia_categoria})

{nombre_apoderado}, abogado en ejercicio identificado con C.C. No. {cedula_apoderado} y
T.P. No. {tarjeta_profesional} del C.S. de la J., obrando en mi calidad de apoderado
especial del señor[a] {nombre_actor}, conforme al poder que se anexa, comedidamente
comparezco ante su Despacho a promover **DEMANDA ORDINARIA LABORAL DE PRIMERA INSTANCIA**
contra **{nombre_demandado}**, con el objeto de que se acceda a las pretensiones
expuestas en el presente libelo.

# I. PRETENSIONES

## Principales

PRIMERA. Que se declare que entre {nombre_actor} y {nombre_demandado} existió un
contrato de trabajo a término {tipo_contrato} desde el {fecha_ingreso} hasta el
{fecha_terminacion}.

SEGUNDA. Que como consecuencia de la declaración anterior, se condene a la demandada
a pagar al actor las sumas que en derecho correspondan por concepto de:

a) Cesantías e intereses sobre cesantías (CST Art. 249, Ley 52/1975).
b) Prima de servicios (CST Art. 306).
c) Vacaciones compensadas (CST Art. 186).
d) Indemnización por terminación unilateral sin justa causa (CST Art. 64
   modificado por Ley 789/2002 Art. 28).
e) Indemnización moratoria (CST Art. 65) por el no pago oportuno de salarios
   y prestaciones a la terminación del contrato.
f) Sanción por no consignación de cesantías al fondo (Ley 50/1990 Art. 99 numeral 3).

TERCERA. Que se condene a la demandada al pago de la **indexación** de las sumas
adeudadas conforme al IPC certificado por el DANE, desde su exigibilidad y hasta el
pago total efectivo.

CUARTA. Que se condene a la demandada al pago de **costas y agencias en derecho**.

## Subsidiarias

PRIMERA SUBSIDIARIA. En caso de no acceder a la pretensión segunda literal d),
solicito se reconozca y pague la indemnización conforme al régimen aplicable según
el tiempo de servicios.

SEGUNDA SUBSIDIARIA. Que se ordene el pago de los intereses moratorios a la tasa
máxima legalmente permitida sobre las sumas reconocidas.

# II. HECHOS

PRIMERO. {nombre_actor} ingresó a prestar sus servicios personales al servicio de
{nombre_demandado} desde el {fecha_ingreso}, mediante contrato de trabajo a término
{tipo_contrato}, desempeñando el cargo de {cargo}.

SEGUNDO. La relación laboral se desarrolló bajo continuada subordinación y
dependencia, recibiendo el actor instrucciones directas de la demandada respecto del
modo, tiempo y lugar de la prestación del servicio.

TERCERO. El último salario mensual percibido por el actor fue de **${salario_mensual}** COP,
pagado en periodicidad mensual, sin perjuicio de los demás factores salariales que se
acreditarán en el curso del proceso.

CUARTO. El día {fecha_terminacion} la demandada dio por terminado unilateralmente el
contrato de trabajo {causa_terminacion}, sin que mediara justa causa de las
contempladas en el Art. 62 del CST.

QUINTO. A la terminación del contrato la demandada no canceló las prestaciones
sociales adeudadas al actor, configurándose la indemnización moratoria del Art. 65 CST.

SEXTO. {hechos_adicionales}

# III. FUNDAMENTOS DE DERECHO

Las pretensiones de la presente demanda se fundamentan en las siguientes normas:

- **Constitución Política**, Art. 25 (derecho al trabajo) y 53 (principios mínimos
  fundamentales del trabajo).
- **Código Sustantivo del Trabajo**, Arts. 22, 23 (elementos del contrato de
  trabajo); 64 modif. Ley 789/2002 Art. 28 (indemnización despido sin justa causa);
  65 (indemnización moratoria); 186, 189 (vacaciones); 249 (cesantías); 306 (prima
  de servicios).
- **Ley 50 de 1990**, Art. 99 (régimen de cesantías y sanción por no consignación).
- **Ley 52 de 1975**, Art. 1 (intereses sobre cesantías).
- **Código General del Proceso (Ley 1564/2012)**, Arts. 281+ (demanda).

**Jurisprudencia aplicable:**

{jurisprudencia_aplicable}

# IV. PRUEBAS

## Documentales

1. Copia de la cédula de ciudadanía del actor.
2. Contrato de trabajo y/o documentos que acrediten el vínculo laboral.
3. Comprobantes de nómina y consignación bancaria correspondientes a los últimos
   doce (12) meses de la relación laboral.
4. Certificación laboral expedida por la demandada (si fue otorgada).
5. {pruebas_documentales_adicionales}

## Testimoniales

Solicito se reciba declaración a las personas que oportunamente indicaré en
audiencia, sobre los hechos relacionados con la prestación del servicio.

## Interrogatorio de parte

Solicito decretar el interrogatorio de parte del representante legal de
{nombre_demandado}, quien deberá absolver el cuestionario que en su oportunidad
allegaré, conforme al CGP Art. 198.

## Inspección judicial

De ser necesario y conforme al CGP Art. 230, solicito decretar inspección judicial
sobre los archivos contables y de personal de la demandada para verificar los
factores salariales y prestacionales.

# V. ANEXOS

1. Poder otorgado al suscrito apoderado.
2. Copia de la cédula de ciudadanía del actor.
3. Documentos de prueba relacionados.
4. Copia de la T.P. del apoderado.

# VI. NOTIFICACIONES

**Demandante:** Recibirá notificaciones por intermedio del suscrito apoderado.

**Demandado:** {direccion_demandado}, en {ciudad_demandado}.

**Apoderado:** {direccion_apoderado}, correo electrónico {email_apoderado},
teléfono {telefono_apoderado}.

# VII. JURAMENTO ESTIMATORIO (CGP Art. 206)

Bajo la gravedad del juramento, manifiesto que la cuantía de las pretensiones
asciende a la suma de **${cuantia}** COP, conforme a la liquidación que se aporta
como anexo.

Cordialmente,

_______________________________
{nombre_apoderado}
C.C. No. {cedula_apoderado}
T.P. No. {tarjeta_profesional} del C.S. de la J.
"""


# ════════════════════════════════════════════════════════════════════════
# 2 · ACCIÓN DE TUTELA · Art. 86 C.P. + Decreto 2591/1991
# ════════════════════════════════════════════════════════════════════════

ACCION_DE_TUTELA = """\
{fecha}

Señor[a]
JUEZ ({juez_competente}) DE BOGOTÁ (REPARTO)
E. S. D.

**ACCIÓN DE TUTELA**
**ACCIONANTE:** {nombre_accionante}, identificado(a) con C.C. No. {cedula_accionante}
**ACCIONADO:** {nombre_accionado}
**DERECHOS FUNDAMENTALES VULNERADOS:** {derechos_vulnerados}

{nombre_accionante}, identificado(a) con la cédula de ciudadanía relacionada,
domiciliado(a) en {direccion_accionante}, en ejercicio del derecho establecido en
el **Artículo 86 de la Constitución Política** y reglamentado por el **Decreto
2591 de 1991**, comedidamente acudo ante su Despacho para promover **ACCIÓN DE
TUTELA** contra **{nombre_accionado}**, en virtud de la vulneración de mis derechos
fundamentales a {derechos_vulnerados}, con base en los siguientes:

# I. HECHOS

PRIMERO. {hecho_1}

SEGUNDO. {hecho_2}

TERCERO. {hecho_3}

CUARTO. Como consecuencia de la conducta del accionado, se vulneran de forma actual
y directa mis derechos fundamentales a {derechos_vulnerados}, ante lo cual no
existe otro mecanismo idóneo y efectivo para su protección, o de existir, se acude
a la presente acción de tutela como mecanismo transitorio para evitar un
**perjuicio irremediable**.

# II. DERECHOS FUNDAMENTALES VULNERADOS

Considero vulnerados los siguientes derechos fundamentales reconocidos en la
Constitución Política:

- {derecho_1} (Art. {articulo_constitucional_1} C.P.)
- {derecho_2} (Art. {articulo_constitucional_2} C.P.)

Sustento la procedencia y prosperidad de la presente acción en la jurisprudencia
de la H. Corte Constitucional, en especial:

{jurisprudencia_aplicable}

# III. JURAMENTO

Bajo la gravedad del juramento y de conformidad con el **Art. 37 del Decreto
2591 de 1991**, manifiesto que **NO he interpuesto otra acción de tutela por los
mismos hechos y derechos** ni ante este ni ante otro despacho judicial.

# IV. PRETENSIONES

PRIMERA. Tutelar mis derechos fundamentales a {derechos_vulnerados}.

SEGUNDA. En consecuencia, ordenar a {nombre_accionado} que en un plazo no superior
a {plazo_horas} horas: {orden_solicitada}.

TERCERA. Conminar al accionado para que se abstenga de incurrir nuevamente en
las conductas que dieron origen a la presente acción.

CUARTA. Las demás medidas que el Despacho considere pertinentes para garantizar
la efectividad del fallo, conforme al Art. 27 del Decreto 2591/1991.

# V. PRUEBAS

1. Copia de mi cédula de ciudadanía.
2. {prueba_1}
3. {prueba_2}

# VI. NOTIFICACIONES

**Accionante:** {direccion_accionante}, correo {email_accionante},
teléfono {telefono_accionante}.

**Accionado:** {direccion_accionado}.

Cordialmente,

_______________________________
{nombre_accionante}
C.C. No. {cedula_accionante}
"""


# ════════════════════════════════════════════════════════════════════════
# 3 · CONTESTACIÓN DE DEMANDA · CGP Art. 96+
# ════════════════════════════════════════════════════════════════════════

CONTESTACION = """\
{fecha}

Señor[a]
JUEZ {juzgado_origen}
E. S. D.

**REFERENCIA:** PROCESO {tipo_proceso} · Expediente No. {expediente}
**DEMANDANTE:** {nombre_demandante}
**DEMANDADO:** {nombre_demandado_yo}

{nombre_apoderado}, abogado en ejercicio identificado con T.P. No. {tarjeta_profesional}
del C.S. de la J., obrando como apoderado especial de {nombre_demandado_yo}, dentro
del término de traslado y conforme al **Art. 96 del CGP**, presento **CONTESTACIÓN
DE LA DEMANDA** instaurada en mi contra, en los siguientes términos:

# I. AL RESPECTO DE LOS HECHOS

A continuación me pronuncio sobre cada uno de los hechos de la demanda:

**Al hecho PRIMERO:** {pronunciamiento_hecho_1}

**Al hecho SEGUNDO:** {pronunciamiento_hecho_2}

**Al hecho TERCERO:** {pronunciamiento_hecho_3}

# II. AL RESPECTO DE LAS PRETENSIONES

Me opongo formalmente a la totalidad de las pretensiones de la demanda, por
carecer de fundamento fáctico y jurídico, conforme al desarrollo que se hace en
las excepciones a continuación.

# III. EXCEPCIONES DE MÉRITO

**PRIMERA. {nombre_excepcion_1}.** {fundamento_excepcion_1}

**SEGUNDA. {nombre_excepcion_2}.** {fundamento_excepcion_2}

**TERCERA. EXCEPCIÓN GENÉRICA.** Toda otra excepción que aparezca probada en el
curso del proceso, conforme al CGP.

# IV. FUNDAMENTOS DE DERECHO

{fundamentos_derecho}

**Jurisprudencia aplicable:**

{jurisprudencia_aplicable}

# V. PRUEBAS

## Documentales

1. {prueba_documental_1}
2. {prueba_documental_2}

## Testimoniales

Solicito se reciba declaración de las personas que oportunamente indicaré.

## Interrogatorio de parte

Solicito decretar interrogatorio de parte al demandante {nombre_demandante}.

# VI. NOTIFICACIONES

**Apoderado:** {direccion_apoderado}, correo {email_apoderado},
teléfono {telefono_apoderado}.

Cordialmente,

_______________________________
{nombre_apoderado}
T.P. No. {tarjeta_profesional} del C.S. de la J.
"""


# ════════════════════════════════════════════════════════════════════════
# 4 · RECURSO DE APELACIÓN · CGP Art. 320+
# ════════════════════════════════════════════════════════════════════════

RECURSO_APELACION = """\
{fecha}

Señor[a]
JUEZ {juzgado_origen}
E. S. D.

**REFERENCIA:** {tipo_proceso} · Expediente No. {expediente}
**DEMANDANTE:** {nombre_demandante}
**DEMANDADO:** {nombre_demandado}

{nombre_apoderado}, abogado en ejercicio con T.P. No. {tarjeta_profesional}, dentro
del término legal y conforme al **CGP Art. 322**, interpongo **RECURSO DE APELACIÓN**
contra la sentencia de primera instancia proferida el {fecha_sentencia} por su
Despacho, en los siguientes términos:

# I. PROVIDENCIA QUE SE APELA

Sentencia de primera instancia de fecha {fecha_sentencia}, proferida en el
proceso de la referencia, mediante la cual el Despacho {sentido_sentencia}.

# II. INCONFORMIDADES (CGP Art. 322)

A continuación se sustentan los puntos específicos de inconformidad:

## 1. {inconformidad_1_titulo}

{inconformidad_1_argumento}

## 2. {inconformidad_2_titulo}

{inconformidad_2_argumento}

## 3. {inconformidad_3_titulo}

{inconformidad_3_argumento}

# III. JURISPRUDENCIA APLICABLE

{jurisprudencia_aplicable}

# IV. PRETENSIÓN DEL RECURSO

Conforme a lo expuesto, comedidamente solicito al H. Tribunal:

PRIMERA. **REVOCAR** la sentencia apelada en cuanto {pretension_revocacion}.

SEGUNDA. **EN SU LUGAR**, {pretension_subsidiaria}.

TERCERA. Las demás determinaciones que en derecho correspondan.

# V. NOTIFICACIONES

**Apoderado:** {direccion_apoderado}, correo {email_apoderado}.

Cordialmente,

_______________________________
{nombre_apoderado}
T.P. No. {tarjeta_profesional} del C.S. de la J.
"""


# ════════════════════════════════════════════════════════════════════════
# 5 · DERECHO DE PETICIÓN · Art. 23 C.P. + Ley 1755/2015
# ════════════════════════════════════════════════════════════════════════

DERECHO_DE_PETICION = """\
{fecha}

Señor[a]
{nombre_destinatario}
{cargo_destinatario}
{entidad_destinatario}
{direccion_destinatario}

**REFERENCIA:** Derecho de petición · {asunto}

{nombre_peticionario}, identificado(a) con C.C. No. {cedula_peticionario}, domiciliado(a)
en {direccion_peticionario}, en ejercicio del derecho fundamental establecido en el
**Art. 23 de la Constitución Política** y conforme al procedimiento establecido en
la **Ley 1755 de 2015**, comedidamente formulo derecho de petición en los siguientes
términos:

# 1. HECHOS

PRIMERO. {hecho_1}

SEGUNDO. {hecho_2}

# 2. PETICIÓN CONCRETA

Con fundamento en los hechos anteriores, solicito a usted:

PRIMERO. {peticion_1}

SEGUNDO. {peticion_2}

# 3. FUNDAMENTOS DE DERECHO

- **Art. 23 Constitución Política** — Derecho fundamental de petición.
- **Ley 1755 de 2015** — Términos de respuesta: 15 días hábiles para peticiones
  ordinarias; 10 para documentos y consultas; 30 para consultas por funciones.

# 4. NOTIFICACIONES

Recibiré notificaciones en {direccion_peticionario}, correo electrónico
{email_peticionario}, teléfono {telefono_peticionario}.

Atentamente,

_______________________________
{nombre_peticionario}
C.C. No. {cedula_peticionario}
"""


# ════════════════════════════════════════════════════════════════════════
# 6 · CARTA DE REQUERIMIENTO PREVIO
# ════════════════════════════════════════════════════════════════════════

CARTA_REQUERIMIENTO = """\
{fecha}

Señor[a]
{nombre_destinatario}
{direccion_destinatario}

**REFERENCIA:** Requerimiento previo extrajudicial · {asunto}

Estimado[a] {tratamiento_destinatario}:

Por medio de la presente, y obrando en representación de {nombre_cliente},
identificado(a) con {identificacion_cliente}, le notifico formalmente que conforme
a {fundamento_obligacion}, usted adeuda a mi representado(a) la suma de
**${monto_adeudado}** COP, por concepto de {concepto_deuda}.

# Hechos relevantes

1. {hecho_1}
2. {hecho_2}
3. {hecho_3}

# Requerimiento

Por lo expuesto, comedidamente le **REQUIERO** para que en un plazo improrrogable
de **{plazo_dias} días hábiles** contados a partir del recibo de la presente,
proceda a:

a) Pagar la suma adeudada de ${monto_adeudado} COP.
b) {accion_adicional}

# Advertencia

De no atenderse el presente requerimiento dentro del plazo señalado, mi
representado(a) se verá en la necesidad de iniciar las acciones judiciales
correspondientes, incluyendo proceso ejecutivo cuando aplique, con el cobro
adicional de los intereses moratorios a la tasa máxima legal certificada por
la Superintendencia Financiera y las costas procesales correspondientes.

Para mayor claridad, sirvase remitir respuesta al correo electrónico
{email_apoderado} o a la dirección {direccion_apoderado}.

Cordialmente,

_______________________________
{nombre_apoderado}
T.P. No. {tarjeta_profesional} del C.S. de la J.
Apoderado de {nombre_cliente}
"""


# ════════════════════════════════════════════════════════════════════════
# 7 · CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES
# ════════════════════════════════════════════════════════════════════════

CONTRATO_PRESTACION_SERVICIOS = """\
**CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES**

Entre los suscritos a saber, **{nombre_contratante}**, identificado(a) con NIT
{nit_contratante}, representado(a) legalmente por {representante_contratante}, con
C.C. {cedula_representante}, domiciliado(a) en {ciudad_contratante}, quien para
efectos del presente contrato se denominará **EL CONTRATANTE**; y **{nombre_contratista}**,
identificado(a) con C.C. No. {cedula_contratista}, domiciliado(a) en
{ciudad_contratista}, quien para efectos del presente contrato se denominará
**EL CONTRATISTA**, hemos convenido celebrar el presente **CONTRATO DE PRESTACIÓN
DE SERVICIOS PROFESIONALES**, regido por las siguientes:

# CLÁUSULAS

## PRIMERA — OBJETO

EL CONTRATISTA se obliga para con EL CONTRATANTE a prestar, con plena autonomía
técnica y administrativa, los servicios profesionales de {objeto_servicios},
conforme al alcance descrito en el Anexo Técnico que forma parte integral del
presente contrato.

## SEGUNDA — VALOR Y FORMA DE PAGO

El valor total del contrato se pacta en la suma de **${valor_contrato}** COP, los
cuales se cancelarán de la siguiente forma: {forma_pago}.

PARÁGRAFO. Los pagos se realizarán previa presentación de la cuenta de cobro o
factura electrónica, certificación de pago de aportes al sistema de seguridad
social y aprobación del informe correspondiente por parte del supervisor.

## TERCERA — PLAZO

El plazo de ejecución del presente contrato es de **{plazo_meses} meses**, contados
a partir del {fecha_inicio} y hasta el {fecha_terminacion}, sin perjuicio de las
prórrogas que las partes pacten expresamente por escrito.

## CUARTA — OBLIGACIONES DEL CONTRATISTA

EL CONTRATISTA se obliga a:

a) Ejecutar las actividades objeto del contrato con la mayor diligencia y
   profesionalidad.
b) Acreditar el pago mensual de aportes al sistema de seguridad social integral
   conforme al **Art. 244 Ley 1955/2019**.
c) Cumplir los plazos pactados.
d) {obligacion_especifica_1}

## QUINTA — OBLIGACIONES DEL CONTRATANTE

a) Cancelar oportunamente los honorarios pactados.
b) Suministrar la información necesaria para la ejecución del contrato.
c) Designar un supervisor que verifique el cumplimiento.

## SEXTA — INDEPENDENCIA

Las partes manifiestan que el presente contrato es de naturaleza civil/comercial,
sin que entre ellas exista subordinación laboral. En consecuencia, EL CONTRATANTE
no asume ninguna obligación de carácter laboral con EL CONTRATISTA conforme al
**Art. 32 CST** y la jurisprudencia vigente sobre el tema.

## SÉPTIMA — PROPIEDAD INTELECTUAL

Los productos derivados de la ejecución del presente contrato serán de propiedad
exclusiva de EL CONTRATANTE conforme a la **Ley 23 de 1982** y la **Decisión 351
de la CAN**.

## OCTAVA — CONFIDENCIALIDAD

EL CONTRATISTA se obliga a guardar absoluta reserva sobre toda información a la
que tenga acceso con ocasión del presente contrato, obligación que se mantendrá
vigente por dos (2) años contados desde la terminación del mismo.

## NOVENA — TERMINACIÓN

El contrato podrá darse por terminado: (i) por mutuo acuerdo; (ii) por
incumplimiento grave de cualquiera de las partes; (iii) por la culminación del
plazo o del objeto pactado.

## DÉCIMA — CLÁUSULA PENAL

En caso de incumplimiento, la parte infractora pagará a la cumplida una suma
equivalente al **{porcentaje_clausula}%** del valor total del contrato, a título de
cláusula penal pecuniaria, sin perjuicio del derecho a exigir el cumplimiento o
la indemnización de perjuicios adicionales.

## UNDÉCIMA — SOLUCIÓN DE CONTROVERSIAS

Las controversias derivadas del presente contrato se resolverán: (i) primeramente
por arreglo directo en un plazo máximo de quince (15) días; (ii) en su defecto,
mediante conciliación ante centro autorizado; (iii) finalmente, por la
jurisdicción ordinaria competente en {ciudad_contratante}.

## DUODÉCIMA — PERFECCIONAMIENTO

El presente contrato se perfecciona con la firma de las partes y produce sus
efectos a partir del {fecha_inicio}.

En constancia de lo anterior, las partes firman el presente documento en dos (2)
ejemplares del mismo tenor, en {ciudad_contratante} a los {dia_firma} días del
mes de {mes_firma} del año {anio_firma}.

EL CONTRATANTE                                EL CONTRATISTA

____________________________                  ____________________________
{representante_contratante}                   {nombre_contratista}
C.C. {cedula_representante}                   C.C. No. {cedula_contratista}
en representación de {nombre_contratante}
"""


# ════════════════════════════════════════════════════════════════════════
# 8 · ESCRITO PROCESAL GENÉRICO (memoriales, solicitudes, oficios)
# ════════════════════════════════════════════════════════════════════════

ESCRITO_GENERICO = """\
{fecha}

Señor[a]
JUEZ {juzgado}
E. S. D.

**REFERENCIA:** {tipo_proceso} · Expediente No. {expediente}
**ASUNTO:** {asunto}

{nombre_apoderado}, abogado en ejercicio con T.P. No. {tarjeta_profesional},
obrando como apoderado de {parte_representada} dentro del proceso de la referencia,
respetuosamente expongo:

# Hechos

PRIMERO. {hecho_1}

SEGUNDO. {hecho_2}

# Solicitud

Por lo anteriormente expuesto, comedidamente solicito a su Despacho:

PRIMERO. {solicitud_1}

SEGUNDO. {solicitud_2}

# Fundamentos de derecho

{fundamentos_derecho}

# Notificaciones

**Apoderado:** {direccion_apoderado}, correo {email_apoderado}.

Cordialmente,

_______________________________
{nombre_apoderado}
T.P. No. {tarjeta_profesional} del C.S. de la J.
"""


# ─────────────────────────────────────────────────────────────────────
# Registry público
# ─────────────────────────────────────────────────────────────────────

TEMPLATES: dict[str, dict] = {
    "demanda_ordinaria_laboral": {
        "title": "Demanda Ordinaria Laboral",
        "description": "Demanda CST + CGP para pretensiones laborales (cesantías, indemnizaciones, prima)",
        "applicable": "laboral",
        "markdown": DEMANDA_ORDINARIA_LABORAL,
    },
    "tutela": {
        "title": "Acción de Tutela",
        "description": "Art. 86 C.P. + Decreto 2591/1991. Protección de derechos fundamentales",
        "applicable": "constitucional",
        "markdown": ACCION_DE_TUTELA,
    },
    "contestacion": {
        "title": "Contestación de Demanda",
        "description": "CGP Art. 96. Contestación con excepciones de mérito",
        "applicable": "civil|laboral|comercial",
        "markdown": CONTESTACION,
    },
    "recurso_apelacion": {
        "title": "Recurso de Apelación",
        "description": "CGP Art. 320+. Apelación con sustentación de inconformidades",
        "applicable": "civil|laboral|comercial|administrativo",
        "markdown": RECURSO_APELACION,
    },
    "derecho_peticion": {
        "title": "Derecho de Petición",
        "description": "Art. 23 C.P. + Ley 1755/2015. Petición ante autoridad/particular",
        "applicable": "constitucional|administrativo",
        "markdown": DERECHO_DE_PETICION,
    },
    "carta_requerimiento": {
        "title": "Carta de Requerimiento",
        "description": "Cobro pre-judicial / requerimiento extrajudicial",
        "applicable": "civil|comercial|laboral",
        "markdown": CARTA_REQUERIMIENTO,
    },
    "contrato": {
        "title": "Contrato de Prestación de Servicios",
        "description": "Contrato civil/comercial con cláusulas estándar (objeto, valor, plazo, propiedad intelectual)",
        "applicable": "comercial|civil",
        "markdown": CONTRATO_PRESTACION_SERVICIOS,
    },
    "escrito": {
        "title": "Escrito procesal genérico",
        "description": "Memorial, solicitud o pronunciamiento dentro de proceso en curso",
        "applicable": "civil|laboral|comercial|penal|administrativo",
        "markdown": ESCRITO_GENERICO,
    },
}


def render_template(kind: str, facts: Optional[dict] = None) -> str:
    """Sustituye placeholders {x} en la plantilla con valores de `facts`.

    Si un placeholder no está en `facts`, queda como `[CAMPO_X]` para que el
    abogado lo rellene en el editor.
    """
    if kind not in TEMPLATES:
        raise ValueError(f"kind '{kind}' no existe. Opciones: {list(TEMPLATES.keys())}")
    facts = dict(facts or {})
    facts.setdefault("fecha", _today_es())

    md = TEMPLATES[kind]["markdown"]

    # Sustituir placeholders presentes en facts
    out = md
    import re
    placeholder_re = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

    def repl(m):
        key = m.group(1)
        val = facts.get(key)
        if val is None or val == "":
            return f"[{key.upper().replace('_', ' ')}]"
        return str(val)

    out = placeholder_re.sub(repl, out)
    return out


def list_templates() -> list[dict]:
    """Devuelve la lista de plantillas disponibles para el dropdown UI."""
    return [
        {
            "kind": k,
            "title": t["title"],
            "description": t["description"],
            "applicable": t["applicable"],
        }
        for k, t in TEMPLATES.items()
    ]
