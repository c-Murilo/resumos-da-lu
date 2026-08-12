# Container único: Flask serve a API e o PWA buildado.
# Precisa de Node porque o servidor MCP da Plaud é um pacote npm.

FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# O front chama a API no mesmo domínio, então não há CORS nem URL para configurar.
ENV VITE_API_URL=/api
RUN npm run build


FROM python:3.12-slim

# Node fica no runtime: o backend sobe o MCP da Plaud a cada chamada.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Usuário sem privilégios com HOME próprio: é onde o MCP grava ~/.plaud.
RUN useradd --create-home --shell /bin/bash app
ENV HOME=/home/app
WORKDIR /app

# Versão fixa e instalada localmente: sem isso, cada request consultaria o
# registro do npm antes de rodar o MCP.
ARG PLAUD_MCP_VERSION=0.3.8
RUN npm install -g @plaud-ai/mcp@${PLAUD_MCP_VERSION}
ENV PLAUD_MCP_COMMAND=plaud-mcp \
    PLAUD_MCP_ARGS=""

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY backend/ ./
COPY --from=frontend /frontend/dist ./static

RUN chown -R app:app /app /home/app
USER app

ENV PORT=8080 \
    PYTHONUNBUFFERED=1
EXPOSE 8080

# Um worker só: o resumo automático roda numa thread e não pode duplicar.
# Threads dão conta da espera de I/O, que é o que este app faz o tempo todo.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 1800 app:app"]
