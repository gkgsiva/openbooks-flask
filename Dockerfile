FROM python:3.12-slim

# openbooks binary (linux amd64) — copy from repo or download at build time
ARG OPENBOOKS_VERSION=v4.5.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -sL -o /usr/local/bin/openbooks \
      https://github.com/evan-buss/openbooks/releases/download/${OPENBOOKS_VERSION}/openbooks_linux && \
    chmod +x /usr/local/bin/openbooks && \
    apt-get purge -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY venv-requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ templates/

ENV OPENBOOKS_BINARY=/usr/local/bin/openbooks \
    OPENBOOKS_URL=ws://127.0.0.1:8080/ws \
    OPENBOOKS_PORT=8080 \
    OPENBOOKS_DOWNLOAD_DIR=/tmp/openbooks

EXPOSE 5000
CMD ["sh", "-c", "exec python app.py"]
