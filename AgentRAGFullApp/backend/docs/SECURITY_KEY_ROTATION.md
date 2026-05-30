# Rotación de claves Anthropic · checklist

**Sprint:** M20.01
**Aplica a:** Anthropic API Key (y por extensión: OpenAI, Supabase service_role)

## Cuándo rotar

- **OBLIGATORIO inmediato:** clave compartida fuera de canales seguros (chat, screenshot, repo público).
- Cada 90 días (rotación preventiva).
- Cuando un colaborador deja el equipo.
- Después de cualquier incidente de seguridad.

## Pasos para Anthropic API Key

### 1. Generar nueva key
- Ir a https://console.anthropic.com/settings/keys
- Click "Create Key"
- Nombre: `lexai-prod-YYYY-MM-DD`
- Permisos: lectura+escritura sobre `claude-sonnet-4-6` y `claude-opus-4-7`
- Copiar la key (solo se muestra una vez)

### 2. Actualizar en Railway producción
```bash
railway login
railway link <project>
railway variables --set ANTHROPIC_API_KEY=sk-ant-...
railway redeploy
```
Esperar ~30s a que el deploy se complete. Verificar con:
```bash
curl https://legal-agent-backend-production-fcfa.up.railway.app/health
```

### 3. Actualizar en .env local de cada dev
- Backend: `Legal_agent_backend/AgentRAGFullApp/backend/.env`
- Frontend: `Legal_agent_Frontend/.env.local` (si aplica)

Variable:
```
ANTHROPIC_API_KEY=sk-ant-...
```

### 4. Smoke test
```bash
cd backend
python scripts/smoke_openai_latency.py   # también prueba Anthropic
```
Esperado: respuesta < 5s, sin 401.

### 5. Revocar la key vieja
- Volver a https://console.anthropic.com/settings/keys
- Click "Revoke" sobre la key antigua
- Confirmar con nombre exacto de la key

### 6. Verificar logs
- En Railway: buscar `401 Unauthorized` en últimas 24h con la nueva key activa
- En Anthropic console: verificar usage en la nueva key (debería aparecer tras smoke test)

### 7. Actualizar este doc
- Editar fecha al inicio: `# Última rotación: YYYY-MM-DD`

## Pasos para OpenAI API Key

Análogo: console.openai.com → API Keys → revocar vieja, crear nueva, actualizar Railway + .env local.

## Pasos para Supabase service_role

**IMPORTANTE:** rotar service_role rompe TODO el backend hasta que se actualice.

1. Coordinar ventana de mantenimiento (5 min downtime esperado)
2. Ir a Supabase Dashboard → Settings → API → "Reset service_role secret"
3. Copiar nuevo `service_role` key
4. Actualizar Railway var `SUPABASE_SERVICE_KEY` + `.env`
5. Redeploy inmediato

## Lo que NUNCA hay que hacer

- Compartir keys en chat, Slack, Discord, GitHub Issues o PRs.
- Pegar keys en logs.
- Subir keys a git (verificar `.env*` está en `.gitignore`).
- Usar la misma key entre dev / staging / prod.
- Dejar keys revocadas en `.env.example` (usar placeholders).

## Audit trail

Cada rotación queda registrada en:
- `audit_logs` con `action='security.key_rotation'`
- Git commit en este doc actualizando fecha

## Última rotación

- **Anthropic:** PENDIENTE (key compartida en chat 2026-05-29 → rotar antes de S2 deploy)
- **OpenAI:** sin rotación pendiente
- **Supabase service_role:** sin rotación pendiente
