FROM python:3.11-slim

WORKDIR /app

# Install system deps for lxml, C extensions, and Node.js 20 for Claude renderer (camino B)
# Node 20 LTS + npm via NodeSource. ca-certificates+curl+gnupg needed to fetch keyring.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
        curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# Pre-install docx-js@9.5 in /opt/docx-runtime so the sandbox (lex/renderer/node_executor.py)
# can require it without re-installing per call. Kept outside /app to avoid being shadowed
# by COPY . . below.
RUN mkdir -p /opt/docx-runtime \
    && cd /opt/docx-runtime \
    && npm init -y >/dev/null \
    && npm install --omit=dev --no-audit --no-fund docx@9.5.1 \
    && node -e "const d=require('docx'); console.log('docx ok ->', Object.keys(d).slice(0,3).join(','));"

# Make the runtime importable via NODE_PATH for the sandbox
ENV NODE_PATH=/opt/docx-runtime/node_modules

# Install CPU-only torch FIRST to avoid pulling CUDA (~2GB)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
