# Agent RAG Full App

Aplicación completa: **Agente RAG con OpenAI + Supabase**, frontend tipo ChatGPT.

```
┌─────────────────────────────────────────────────────────┐
│                     ARQUITECTURA                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │   FRONTEND  │    │   BACKEND   │    │  SUPABASE   │  │
│  │             │    │             │    │             │  │
│  │  React 18   │◄──►│  FastAPI    │◄──►│  PostgreSQL │  │
│  │  Vite 6     │    │  Python     │    │  + pgvector │  │
│  │  Tailwind 4 │    │  Pydantic   │    │             │  │
│  │  shadcn/ui  │    │             │    │             │  │
│  │             │    │  ┌────────┐ │    │             │  │
│  │  port 5173  │    │  │OpenAI  │ │    │  Pooler:    │  │
│  │             │    │  │ API    │ │    │  us-west-2  │  │
│  │             │    │  └────────┘ │    │             │  │
│  └─────────────┘    └─────────────┘    └─────────────┘  │
│                          port 8000                       │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## Características

### Backend (Python / FastAPI)
- **Ingesta nivel 3:** 53+ formatos (PDF, DOCX, código, audio, imágenes, etc.)
- **Retrieval nivel 5:** RAG-first con re-ranking, query expansion, multi-query, self-reflection
- **Memoria conversacional:** las conversaciones se evalúan y se almacenan en el RAG
- **Streaming SSE** de respuestas
- **Sesiones persistentes** en Supabase
- **6 estrategias RAG configurables** vía YAML

### Frontend (React 18 / Vite / Tailwind 4)
- **UI minimalista** estilo ChatGPT
- **Streaming** de respuestas en tiempo real
- **Sidebar de sesiones** con búsqueda y eliminación
- **Panel de conocimiento** con drag & drop de archivos
- **Panel de configuración** del agente
- **Modo oscuro/claro**
- **Animaciones** con Motion (Framer Motion)
- **Toasts** con Sonner
- **Iconos** Lucide React

## Quick Start

### 1. Pre-requisitos

- Node.js >= 18
- Python >= 3.10
- Una clave de OpenAI
- Un proyecto Supabase

### 2. Instalación

**Backend:**
```bash
cd backend
pip install -r requirements.txt
```

**Frontend:**
```bash
cd frontend
npm install
```

### 3. Variables de entorno

**Backend** (`backend/.env`) — ya configurado con tu Supabase y OpenAI:
```env
OPENAI_API_KEY=sk-proj-...
DATABASE_URL=postgresql://postgres.<ref>:<pwd>@aws-1-us-west-2.pooler.supabase.com:6543/postgres
SUPABASE_URL=https://<ref>.supabase.co
HOST=0.0.0.0
PORT=8000
```

**Frontend** (`frontend/.env`):
```env
VITE_API_BASE_URL=http://localhost:8000
```

### 4. Inicializar base de datos (ya hecha)

El esquema ya fue ejecutado en Supabase. Si quieres re-inicializar:
```bash
cd backend
python cli.py init-db
```

Esto crea: `documents`, `chunks`, `conversations`, `conversation_chunks` + funciones SQL `match_chunks`, `hybrid_search`, `match_conversations`, `match_all`.

### 5. Iniciar la app

**Opción A: Launcher (recomendado)**

Windows:
```cmd
start.bat
```

macOS / Linux / Git Bash:
```bash
./start.sh
```

**Opción B: Manual (2 terminales)**

Terminal 1 (backend):
```bash
cd backend
python main.py
```

Terminal 2 (frontend):
```bash
cd frontend
npm run dev
```

### 6. Abrir la app

- **Frontend:** http://localhost:5173
- **API:** http://localhost:8000
- **API docs (Swagger):** http://localhost:8000/docs

## Uso

### Chat
1. Abre http://localhost:5173
2. Escribe un mensaje en la caja de input
3. El agente responde con streaming en tiempo real
4. Las sesiones se guardan automáticamente y aparecen en el sidebar

### Subir documentos al RAG
1. Click en el icono de libro (📖) en el navbar o footer del sidebar
2. Arrastra archivos o haz clic para seleccionar
3. Soporta: PDF, DOCX, XLSX, MD, TXT, código fuente, CSV, JSON, audio, etc.
4. Los archivos se procesan, embebidos y guardados en Supabase
5. El agente automáticamente usa este conocimiento al responder

### Sesiones
- Cada conversación tiene un `session_id` único
- Las sesiones aparecen en el sidebar ordenadas por fecha
- Click en una sesión para cargarla
- Click en el icono de papelera para eliminarla
- "Nueva conversación" crea un nuevo session_id

### Settings
- Click en el icono ⚙ (engranaje) del navbar
- Ajusta:
  - Modelo principal y utilitario
  - Temperature
  - Estrategias RAG (toggle individual)
  - Memoria conversacional
  - Modo oscuro

## Estructura del proyecto

```
AgentRAGFullApp/
│
├── backend/                    ← Python / FastAPI
│   ├── main.py                 ← Entry point del servidor
│   ├── cli.py                  ← CLI (init-db, ingest, chat)
│   ├── .env                    ← Variables (OpenAI + Supabase)
│   ├── requirements.txt
│   ├── config/                 ← Configuración Pydantic + YAML
│   │   ├── default.yaml
│   │   └── schema.py
│   ├── api/                    ← Endpoints FastAPI
│   │   ├── chat.py             ← POST /api/chat (streaming)
│   │   ├── sessions.py         ← CRUD de sesiones
│   │   ├── ingest.py           ← Upload de archivos
│   │   ├── documents.py        ← CRUD de documentos
│   │   └── health.py
│   ├── agent/                  ← Agente RAG
│   │   ├── base_agent.py       ← Stateless RAG agent
│   │   ├── system_prompts.py
│   │   ├── response_builder.py
│   │   └── tools/
│   ├── retrieval/              ← Pipeline de consulta (Nivel 5)
│   │   ├── pipeline.py
│   │   ├── intent_router.py
│   │   ├── query_expansion.py
│   │   ├── multi_query.py
│   │   ├── reranker.py
│   │   ├── self_reflection.py
│   │   └── sql_generator.py
│   ├── ingestion/              ← Pipeline de ingesta (Nivel 3)
│   │   ├── pipeline.py
│   │   ├── format_router.py
│   │   ├── readers/            ← 7 readers
│   │   ├── chunkers/           ← 5 chunkers
│   │   ├── enrichment.py
│   │   ├── embedder.py
│   │   └── conversation_memory.py
│   ├── storage/
│   │   ├── base.py             ← Interface
│   │   ├── postgres.py         ← Supabase / pgvector
│   │   └── schemas/init.sql    ← DDL
│   ├── models/                 ← Pydantic / dataclasses
│   └── utils/
│
├── frontend/                   ← React 18 / Vite / Tailwind 4
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── .env                    ← VITE_API_BASE_URL
│   └── src/
│       ├── main.tsx
│       ├── lib/
│       │   └── api.ts          ← Cliente HTTP del backend
│       ├── styles/
│       │   ├── theme.css       ← Tokens de diseño
│       │   ├── tailwind.css
│       │   └── index.css
│       └── app/
│           ├── App.tsx         ← Componente raíz
│           └── components/
│               ├── Sidebar.tsx
│               ├── Navbar.tsx
│               ├── ChatArea.tsx
│               ├── ChatMessage.tsx
│               ├── MessageInput.tsx
│               ├── KnowledgePanel.tsx
│               ├── SettingsPanel.tsx
│               └── ui/         ← Componentes shadcn/ui
│
├── start.bat                   ← Launcher Windows
├── start.sh                    ← Launcher bash
├── .gitignore
└── README.md
```

## Endpoints de la API

| Método | Path | Descripción |
|---|---|---|
| GET | `/api/health` | Health check |
| POST | `/api/chat/` | Enviar mensaje (JSON o streaming) |
| POST | `/api/chat/reset` | Generar nuevo session_id |
| GET | `/api/sessions/` | Listar sesiones |
| GET | `/api/sessions/{id}` | Obtener mensajes de sesión |
| DELETE | `/api/sessions/{id}` | Eliminar sesión |
| POST | `/api/ingest/text` | Ingestar texto raw |
| POST | `/api/ingest/files` | Subir y procesar archivos |
| POST | `/api/ingest/directory` | Ingestar directorio del servidor |
| GET | `/api/documents/` | Listar documentos |
| GET | `/api/documents/{id}` | Obtener documento |
| DELETE | `/api/documents/{id}` | Eliminar documento |
| DELETE | `/api/documents/` | Borrar todos los documentos |

## Tecnologías

### Backend
- **FastAPI** — framework web async
- **Pydantic v2** — validación
- **asyncpg** — driver Postgres async (con `statement_cache_size=0` para Supabase Pooler)
- **OpenAI SDK** — chat completions y embeddings
- **Docling** — procesamiento de documentos
- **sentence-transformers** — re-ranking con cross-encoder

### Frontend
- **React 18** + **TypeScript 5**
- **Vite 6** — bundler
- **Tailwind CSS 4** — styling
- **Radix UI** — primitives accesibles
- **Motion** — animaciones
- **Sonner** — toasts
- **Lucide React** — iconos

### Infraestructura
- **Supabase** — Postgres + pgvector (Transaction Pooler us-west-2)
- **OpenAI** — gpt-4o, gpt-4o-mini, text-embedding-3-small

## Notas importantes

### Seguridad
- **Las credenciales en `.env` NO deben subirse a git** (`.gitignore` ya lo hace)
- Si compartiste estas credenciales públicamente, **rótalas inmediatamente**:
  - OpenAI: https://platform.openai.com/api-keys
  - Supabase: Settings → Database → Reset password
  - Supabase: Settings → API → Re-generate keys

### Supabase Pooler
- Se usa el **Transaction Pooler** (`aws-1-us-west-2.pooler.supabase.com:6543`)
- `statement_cache_size=0` es **obligatorio** porque el pooler no soporta prepared statements
- Esto ya está configurado en `storage/postgres.py`

### Memoria conversacional
- Cada exchange usuario↔agente es evaluado por gpt-4o-mini con un score 1-5
- Solo se almacenan exchanges con score ≥ 3 (configurable)
- Los exchanges almacenados se embeben y se incluyen en futuras búsquedas RAG

## Customización

Para crear un agente especializado (ej. Director Comercial), edita `backend/config/default.yaml`:

```yaml
agent:
  name: "Director Comercial"
  role: "Director Comercial con experiencia en B2B"
  primary_model: "gpt-4o"
  temperature: 0.3
  db_tables_schema: |
    - leads (id, name, email, status, value, created_at)
    - quotations (id, lead_id, amount, status, valid_until)
```

El agente automáticamente generará SQL seguro cuando los usuarios pregunten por métricas de tu BD.

## Troubleshooting

**Backend no arranca:**
- Verifica `OPENAI_API_KEY` y `DATABASE_URL` en `backend/.env`
- Test rápido: `cd backend && python -c "from main import app; print('OK')"`

**Error "Tenant or user not found" desde Supabase:**
- Verifica que el `DATABASE_URL` use el formato `postgres.<project-ref>:<password>@...`
- Confirma la región del pooler en https://supabase.com/dashboard/project/<ref>/settings/database

**El agente responde "No tengo información":**
- Es normal si no has subido documentos. Sube archivos en el panel de conocimiento.

**Frontend no se conecta al backend:**
- Verifica que el backend esté en `http://localhost:8000` (no `127.0.0.1`)
- Verifica `VITE_API_BASE_URL` en `frontend/.env`
- Reinicia el dev server después de cambiar `.env`

## Licencia

Uso interno / educativo. Verifica las licencias de cada librería incluida.
