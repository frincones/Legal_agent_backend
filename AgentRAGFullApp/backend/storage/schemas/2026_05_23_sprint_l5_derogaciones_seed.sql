-- Sprint L5 · seed manual de derogaciones críticas en leyes_normas
-- ================================================================
-- Inserta leyes conocidas con su vigencia/derogación verificable manualmente.
-- El verifier consultará esta tabla en cada query y devolverá estado='superada'
-- para las moduladas/derogadas, con info de la norma sustituta.

-- Primero, asegurar que algunas leyes históricas estén en la tabla con su
-- vigencia correcta · son normas que un abogado citaría y que necesitan
-- una bandera de "ojo: revisar vigencia".

insert into leyes_normas (tipo, numero, anio, citation_ref, titulo, vigencia, fuente_url, fuente, fetched_at, verified_at)
values
  ('LEY', '1437', 2011, 'LEY 1437/2011',
   'CPACA - Código de Procedimiento Administrativo y de lo Contencioso Administrativo',
   'modulada',
   'http://www.secretariasenado.gov.co/senado/basedoc/ley_1437_2011.html',
   'senado', now(), now())
on conflict (citation_ref) do update set
  vigencia = 'modulada',
  titulo = excluded.titulo,
  fuente_url = excluded.fuente_url,
  updated_at = now();

insert into leyes_normas (tipo, numero, anio, citation_ref, titulo, vigencia, fuente_url, fuente, fetched_at, verified_at)
values
  ('LEY', '23', 1991, 'LEY 23/1991',
   'Por la cual se crean mecanismos para descongestionar despachos judiciales - mayormente sustituida por Ley 446/1998',
   'modulada',
   'http://www.secretariasenado.gov.co/senado/basedoc/ley_0023_1991.html',
   'senado', now(), now())
on conflict (citation_ref) do update set
  vigencia = 'modulada',
  titulo = excluded.titulo,
  updated_at = now();

insert into leyes_normas (tipo, numero, anio, citation_ref, titulo, vigencia, fuente_url, fuente, fetched_at, verified_at)
values
  ('LEY', '1755', 2015, 'LEY 1755/2015',
   'Por medio de la cual se regula el Derecho Fundamental de Petición y se sustituye un título del CPACA',
   'vigente',
   'http://www.secretariasenado.gov.co/senado/basedoc/ley_1755_2015.html',
   'senado', now(), now())
on conflict (citation_ref) do update set
  vigencia = 'vigente',
  titulo = excluded.titulo,
  updated_at = now();

insert into leyes_normas (tipo, numero, anio, citation_ref, titulo, vigencia, fuente_url, fuente, fetched_at, verified_at)
values
  ('LEY', '446', 1998, 'LEY 446/1998',
   'Por la cual se adoptan como legislación permanente algunas normas del Decreto 2651 de 1991',
   'vigente',
   'http://www.secretariasenado.gov.co/senado/basedoc/ley_0446_1998.html',
   'senado', now(), now())
on conflict (citation_ref) do update set vigencia = 'vigente', updated_at = now();

-- Marcar Ley 270/1996 (Estatutaria de Administración de Justicia) como vigente pero modulada
insert into leyes_normas (tipo, numero, anio, citation_ref, titulo, vigencia, fuente_url, fuente, fetched_at, verified_at)
values
  ('LEY', '270', 1996, 'LEY 270/1996',
   'Estatutaria de la Administración de Justicia - modulada parcialmente por Ley 1285/2009',
   'modulada',
   'http://www.secretariasenado.gov.co/senado/basedoc/ley_0270_1996.html',
   'senado', now(), now())
on conflict (citation_ref) do update set vigencia = 'modulada', updated_at = now();

-- Verificación
select citation_ref, titulo, vigencia from leyes_normas where vigencia != 'vigente' order by citation_ref;
