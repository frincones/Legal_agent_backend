-- ============================================================
-- Sprint 1 · A · Extend user_role enum (9 effective roles)
-- ============================================================
-- Date: 2026-05-09
-- Why split: Postgres requires ALTER TYPE ADD VALUE to commit
-- before the new value is usable in DDL/DML. We apply this file
-- first, then the schema (B) which depends on the new values.
--
-- Existing values:  admin, lawyer, paralegal, readonly
-- New values:       socio_senior, socio_junior, independiente,
--                   in_house, funcionario_publico, consultor
-- ============================================================

alter type user_role add value if not exists 'socio_senior';
alter type user_role add value if not exists 'socio_junior';
alter type user_role add value if not exists 'independiente';
alter type user_role add value if not exists 'in_house';
alter type user_role add value if not exists 'funcionario_publico';
alter type user_role add value if not exists 'consultor';
