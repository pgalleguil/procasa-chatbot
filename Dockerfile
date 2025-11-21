FROM python:3.11-slim

RUN apt-get update && apt-get install -y curl && \
    curl -L "https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-x86_64" -o /usr/local/bin/docker-compose && \
    chmod +x /usr/local/bin/docker-compose && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD docker-compose -f evolution/docker-compose.yml up -d --build && \
    uvicorn webhook:app --host 0.0.0.0 --port ${PORT:-8000}