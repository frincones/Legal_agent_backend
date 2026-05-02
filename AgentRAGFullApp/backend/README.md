# Agent RAG Template

A production-ready, customizable template for building AI agents with **Level 3 advanced ingestion** and **Level 5 RAG-first retrieval**.

## Features

### Level 3 Ingestion (53+ file formats)
- **Documents**: PDF, DOCX, PPTX, XLSX, HTML (via Docling)
- **Text**: TXT, MD (with YAML frontmatter), RST, LOG
- **Code**: Python, JS, TS, Java, Go, Rust, C++, C#, Ruby, PHP, SQL, etc.
- **Structured**: CSV, JSON, JSONL, XML, YAML, TOML
- **Audio**: MP3, WAV, M4A, FLAC (via Whisper ASR)
- **Subtitles**: SRT, VTT
- **Images**: PNG, JPG, WebP (via Vision LLM, optional)

Pipeline:
1. Format Router (auto-detect by extension)
2. Reader (format-specific extraction to Markdown)
3. Metadata extraction (title, language, dates, etc.)
4. Chunker (auto-selected: hybrid/code/semantic/record/simple)
5. Contextual Enrichment (Anthropic method, +35-49% accuracy)
6. Batch Embedding (OpenAI text-embedding-3-small, with retry & cache)
7. PostgreSQL + pgVector storage

### Level 5 Retrieval (RAG-first with self-reflection)
1. **Intent Router** - Classifies query (knowledge / structured / hybrid / action / conversation)
2. **Query Expansion** - LLM enriches the query for precision
3. **Multi-Query** - Generates 3-4 variations, parallel search
4. **Hybrid Search** - Vector + BM25 text search
5. **Re-ranking** - Cross-encoder for precise top-K selection
6. **Self-Reflection** - LLM grades results, refines query if needed
7. **SQL Generation** - Safe natural-language to SQL for structured data

### Conversational Memory
- Stores relevant conversation exchanges back into the RAG
- LLM evaluates each exchange for "memorability" (1-5 score)
- Past conversations automatically retrieved for future similar queries

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your DATABASE_URL and OPENAI_API_KEY
```

### 3. Initialize the database

```bash
python cli.py init-db
```

This creates the tables: `documents`, `chunks`, `conversations`, `conversation_chunks`
plus the search functions: `match_chunks`, `hybrid_search`, `match_conversations`, `match_all`.

### 4. Ingest documents

```bash
python cli.py ingest ./my_documents --clean
```

### 5. Chat with your agent

**Interactive CLI:**
```bash
python cli.py chat
```

**REST API:**
```bash
python main.py
# API at http://localhost:8000
# Docs at http://localhost:8000/docs
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| POST | `/api/chat/` | Send a message |
| POST | `/api/chat/reset` | Reset conversation session |
| POST | `/api/ingest/text` | Ingest raw text |
| POST | `/api/ingest/files` | Upload and ingest files |
| POST | `/api/ingest/directory` | Ingest a server directory |
| GET | `/api/documents/` | List all documents |
| GET | `/api/documents/{id}` | Get a document |
| DELETE | `/api/documents/{id}` | Delete a document |
| DELETE | `/api/documents/` | Clear all documents |

## Customization

### Change the agent's role

Edit `config/default.yaml`:

```yaml
agent:
  name: "Director Comercial"
  role: "Sales Director with deep business knowledge"
  primary_model: "gpt-4o"
```

### Add structured data queries

```yaml
agent:
  db_tables_schema: |
    - leads (id, name, email, status, value, assigned_to, created_at)
    - quotations (id, lead_id, amount, status, valid_until)
    - orders (id, quotation_id, total, status, created_at)
```

The agent will automatically generate safe SELECT queries when users ask about metrics.

### Add custom domain actions

Edit `agent/tools/action_tools.py`:

```python
async def create_quotation(client_name: str, items: list) -> str:
    # Your business logic here
    return f"Quotation created for {client_name}"
```

### Toggle RAG strategies

Disable any strategy in `config/default.yaml`:

```yaml
retrieval:
  query_expansion:
    enabled: false  # Disable to save costs
  multi_query:
    enabled: false
  reranking:
    enabled: true   # Always recommended
  self_reflection:
    enabled: true   # Quality vs latency tradeoff
```

### Disable contextual enrichment for faster ingestion

```yaml
ingestion:
  enrichment:
    enabled: false  # Skip LLM enrichment per chunk
```

## Architecture

```
Agent-RAG-Template/
├── config/              # Pydantic config + default YAML
├── models/              # Data models (chunks, documents, search, agent)
├── storage/             # PostgreSQL + pgVector + SQL schema
├── ingestion/
│   ├── readers/         # 7 format readers (Docling, text, code, etc.)
│   ├── chunkers/        # 5 chunking strategies
│   ├── format_router    # Auto-detection and routing
│   ├── enrichment       # Contextual enrichment (Anthropic)
│   ├── embedder         # Batch embedding with retry & cache
│   ├── pipeline         # Orchestration
│   └── conversation_memory  # Store conversations back to RAG
├── retrieval/
│   ├── intent_router    # Classify query type
│   ├── query_expansion  # Enrich query
│   ├── multi_query      # Parallel variations
│   ├── reranker         # Cross-encoder
│   ├── self_reflection  # Evaluate + refine
│   ├── sql_generator    # NL → safe SQL
│   └── pipeline         # RAG-first orchestration
├── agent/
│   ├── base_agent       # Main agent class
│   ├── system_prompts   # Prompt templates
│   ├── response_builder # Format output
│   └── tools/           # RAG, DB, and action tools
├── api/                 # FastAPI endpoints
├── utils/               # DB, LLM, logger
├── main.py              # FastAPI server entry point
└── cli.py               # CLI for ingest/chat/init-db
```

## Cost Optimization

The template uses two model tiers for efficiency:

| Model | Used for | Cost |
|-------|----------|------|
| **gpt-4o-mini** (utility) | Intent routing, query expansion, multi-query, self-reflection, SQL generation, conversation evaluation | ~$0.0001 per call |
| **gpt-4o** (primary) | Final response synthesis only | ~$0.005 per call |
| **Cross-encoder** (local) | Re-ranking | $0 (runs locally) |

Average cost per chat: **~$0.003 - $0.01**

## Stack

- **Framework**: FastAPI
- **LLM**: OpenAI (configurable models)
- **Embeddings**: OpenAI text-embedding-3-small (1536D)
- **Vector DB**: PostgreSQL + pgVector (works with Supabase)
- **Re-ranking**: sentence-transformers cross-encoder
- **Document Processing**: Docling
- **Validation**: Pydantic
