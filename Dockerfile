FROM python:3.12-slim

# Dependências de sistema mínimas (Pillow/reportlab não precisam de libs extras pesadas)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libexpat1 \
    libgomp1 \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências Python primeiro (cache de camada)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copia o restante do projeto
COPY backend /app/backend
COPY frontend /app/frontend

ENV PORT=8000
EXPOSE 8000

WORKDIR /app/backend

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
